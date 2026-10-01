import hashlib
import itertools
import json
import logging
import math
import multiprocessing as mp
import os
import threading
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("speaker_service")

model_name = os.getenv("SPEAKER_MODEL", "iic/speech_campplus_sv_zh-cn_16k-common")
max_segment_seconds = max(2.0, float(os.getenv("SPEAKER_SERVICE_MAX_SEGMENT_SECONDS", "12")))
max_pcm_bytes = int(max_segment_seconds * 16000 * 2)
worker_max_requests = max(1, int(os.getenv("SPEAKER_WORKER_MAX_REQUESTS", "20")))
worker_startup_timeout = max(30.0, float(os.getenv("SPEAKER_WORKER_STARTUP_TIMEOUT", "180")))
worker_request_timeout = max(5.0, float(os.getenv("SPEAKER_WORKER_REQUEST_TIMEOUT", "30")))


class SpeakerWorkerError(RuntimeError):
    pass


class SpeakerWorkerCrashed(SpeakerWorkerError):
    pass


def find_embedding(value: Any):
    if isinstance(value, dict):
        for key in ("spk_embedding", "embedding"):
            if key in value:
                return find_embedding(value[key])
        for child in value.values():
            found = find_embedding(child)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(item, (int, float)) for item in value):
            return [float(item) for item in value]
        for child in value:
            found = find_embedding(child)
            if found is not None:
                return found
    if hasattr(value, "detach"):
        return value.detach().cpu().reshape(-1).tolist()
    try:
        import numpy as np
        if isinstance(value, np.ndarray):
            return value.reshape(-1).tolist()
    except ImportError:
        pass
    return None


def pcm_fingerprint(raw: bytes) -> Dict[str, Any]:
    samples = array("h")
    samples.frombytes(raw)
    if os.sys.byteorder != "little":
        samples.byteswap()
    count = len(samples)
    square_sum = sum(int(value) * int(value) for value in samples)
    return {
        "bytes": len(raw),
        "samples": count,
        "duration_ms": count * 1000 // 16000,
        "rms": int(math.sqrt(square_sum / count)) if count else 0,
        "peak": max((abs(int(value)) for value in samples), default=0),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def validate_pcm(raw: bytes) -> None:
    if len(raw) < 3200:
        raise ValueError("PCM segment is too short")
    if len(raw) % 2:
        raise ValueError("PCM segment is misaligned")
    if len(raw) > max_pcm_bytes:
        raise ValueError(f"PCM segment exceeds {max_segment_seconds:g}s limit")


def _worker_main(connection, config: Dict[str, Any]) -> None:
    """Own CAM++ inside a disposable process so native crashes cannot kill HTTP PID 1."""
    try:
        import numpy as np
        from funasr import AutoModel

        name = str(config["model_name"])
        logger.info("speaker_worker_loading generation=%d model=%s", config["generation"], name)
        model = AutoModel(model=name, device="cpu", disable_update=True)
        t = np.arange(16000, dtype=np.float32) / 16000.0
        selftest = (0.08 * np.sin(2 * np.pi * 180 * t)
                    + 0.04 * np.sin(2 * np.pi * 360 * t)).astype(np.float32)
        result = model.generate(input=selftest, fs=16000, is_final=True)
        embedding = find_embedding(result)
        if not embedding:
            raise RuntimeError("CAM++ self-test returned no embedding")
        connection.send({"type": "ready", "embedding_dims": len(embedding)})
    except BaseException as exc:
        try:
            connection.send({"type": "startup_error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            connection.close()
        return

    while True:
        try:
            request = connection.recv()
        except EOFError:
            break
        if request.get("op") == "shutdown":
            break
        request_id = str(request.get("request_id") or "unknown")
        try:
            raw = request["pcm"]
            audio = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            audio *= 1.0 / 32768.0
            result = model.generate(input=audio, fs=16000, is_final=True)
            embedding = find_embedding(result)
            if not embedding:
                raise RuntimeError("CAM++ returned no embedding")
            connection.send({"type": "result", "request_id": request_id,
                             "embedding": embedding})
        except BaseException as exc:
            try:
                connection.send({"type": "inference_error", "request_id": request_id,
                                 "error": f"{type(exc).__name__}: {exc}"})
            except (BrokenPipeError, EOFError, OSError):
                break
    connection.close()


class SpeakerWorker:
    def __init__(self, *, target: Callable = _worker_main,
                 context=None, max_requests: Optional[int] = None,
                 startup_timeout: Optional[float] = None,
                 request_timeout: Optional[float] = None):
        self.context = context or mp.get_context("spawn")
        self.target = target
        self.max_requests = max_requests or worker_max_requests
        self.startup_timeout = startup_timeout or worker_startup_timeout
        self.request_timeout = request_timeout or worker_request_timeout
        self.lock = threading.Lock()
        self.process = None
        self.connection = None
        self.generation = 0
        self.served = 0
        self.embedding_dims = 0
        self.recycle_pending = False

    def start(self) -> None:
        with self.lock:
            self._start_locked()

    def _start_locked(self) -> None:
        existing_alive = bool(self.process and self.process.is_alive())
        self._stop_locked(graceful=existing_alive)
        parent, child = self.context.Pipe(duplex=True)
        self.generation += 1
        config = {"model_name": model_name, "generation": self.generation}
        process = self.context.Process(
            target=self.target,
            args=(child, config),
            name=f"speaker-campplus-{self.generation}",
            daemon=True,
        )
        process.start()
        child.close()
        self.process = process
        self.connection = parent
        if not parent.poll(self.startup_timeout):
            self._stop_locked(graceful=False)
            raise SpeakerWorkerError("CAM++ worker startup timed out")
        try:
            message = parent.recv()
        except EOFError as exc:
            exitcode = process.exitcode
            self._stop_locked(graceful=False)
            raise SpeakerWorkerCrashed(
                f"CAM++ worker exited during startup (exitcode={exitcode})") from exc
        if message.get("type") != "ready":
            error = str(message.get("error") or "unknown startup error")
            self._stop_locked(graceful=False)
            raise SpeakerWorkerError(error)
        self.served = 0
        self.recycle_pending = False
        self.embedding_dims = int(message.get("embedding_dims") or 0)
        logger.info(
            "speaker_worker_ready generation=%d pid=%s embedding_dims=%d max_requests=%d",
            self.generation, process.pid, self.embedding_dims, self.max_requests,
        )

    def embed(self, raw: bytes, request_id: str) -> List[float]:
        with self.lock:
            if self.recycle_pending or self.process is None or not self.process.is_alive():
                reason = "scheduled" if self.recycle_pending else "not_alive"
                logger.info("speaker_worker_restarting reason=%s generation=%d", reason, self.generation)
                self._start_locked()
            process = self.process
            connection = self.connection
            try:
                connection.send({"op": "embed", "request_id": request_id, "pcm": raw})
                if not connection.poll(self.request_timeout):
                    raise TimeoutError(f"CAM++ inference exceeded {self.request_timeout:g}s")
                response = connection.recv()
            except (BrokenPipeError, EOFError, OSError, TimeoutError) as exc:
                process.join(timeout=0.2)
                exitcode = process.exitcode
                logger.error(
                    "speaker_worker_lost request_id=%s generation=%d pid=%s exitcode=%s error=%s",
                    request_id, self.generation, process.pid, exitcode, exc,
                )
                self._start_locked()
                raise SpeakerWorkerCrashed(
                    f"CAM++ worker crashed or timed out (exitcode={exitcode})") from exc
            if response.get("type") != "result":
                error = str(response.get("error") or "CAM++ inference failed")
                logger.error(
                    "speaker_worker_inference_error request_id=%s generation=%d error=%s",
                    request_id, self.generation, error,
                )
                self._start_locked()
                raise SpeakerWorkerError(error)
            self.served += 1
            if self.served >= self.max_requests:
                self.recycle_pending = True
                logger.info(
                    "speaker_worker_recycle_scheduled generation=%d served=%d",
                    self.generation, self.served,
                )
            return [float(value) for value in response.get("embedding") or []]

    def status(self) -> Dict[str, Any]:
        with self.lock:
            process = self.process
            return {
                "ready": bool(process and process.is_alive() and self.connection),
                "pid": process.pid if process else None,
                "generation": self.generation,
                "served": self.served,
                "max_requests": self.max_requests,
                "recycle_pending": self.recycle_pending,
                "embedding_dims": self.embedding_dims,
            }

    def stop(self) -> None:
        with self.lock:
            self._stop_locked(graceful=True)

    def _stop_locked(self, *, graceful: bool) -> None:
        process = self.process
        connection = self.connection
        self.process = None
        self.connection = None
        if process is None:
            return
        if graceful and process.is_alive() and connection is not None:
            try:
                connection.send({"op": "shutdown"})
            except (BrokenPipeError, EOFError, OSError):
                pass
        process.join(timeout=3)
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=2)
        if connection is not None:
            connection.close()
        try:
            process.close()
        except (ValueError, OSError):
            pass


worker_manager: Optional[SpeakerWorker] = None
request_numbers = itertools.count(1)


class Handler(BaseHTTPRequestHandler):
    server_version = "ClearMeetingSpeaker/2.0.9"

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        status = worker_manager.status() if worker_manager else {"ready": False}
        self.send_json(200 if status["ready"] else 503,
                       {"status": "ok" if status["ready"] else "starting",
                        "model": model_name, "worker": status})

    def do_POST(self):
        if self.path != "/embed":
            self.send_error(404)
            return
        request_id = f"spk-{next(request_numbers):08d}"
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("missing or invalid Content-Length")
            if length > max_pcm_bytes:
                self.send_json(413, {"error": f"PCM segment exceeds {max_segment_seconds:g}s limit",
                                     "request_id": request_id})
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete PCM request body")
            validate_pcm(raw)
            fingerprint = pcm_fingerprint(raw)
            logger.info(
                "speaker_request request_id=%s bytes=%d samples=%d duration_ms=%d rms=%d peak=%d sha256=%s",
                request_id, fingerprint["bytes"], fingerprint["samples"],
                fingerprint["duration_ms"], fingerprint["rms"], fingerprint["peak"],
                fingerprint["sha256"],
            )
            if worker_manager is None:
                raise SpeakerWorkerError("CAM++ worker is not initialized")
            embedding = worker_manager.embed(raw, request_id)
            if not embedding:
                raise SpeakerWorkerError("CAM++ returned no embedding")
            logger.info("speaker_request_done request_id=%s dims=%d", request_id, len(embedding))
            self.send_json(200, {"embedding": embedding, "request_id": request_id})
        except ValueError as exc:
            logger.warning("speaker_request_rejected request_id=%s error=%s", request_id, exc)
            self.send_json(400, {"error": str(exc), "request_id": request_id})
        except Exception as exc:
            logger.exception("embedding_failed request_id=%s error=%s", request_id, exc)
            self.send_json(500, {"error": str(exc), "request_id": request_id})

    def log_message(self, fmt, *args):
        logger.info("request " + fmt, *args)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    global worker_manager
    worker_manager = SpeakerWorker()
    worker_manager.start()
    port = int(os.getenv("SPEAKER_PORT", "10100"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    logger.info("speaker_service_ready port=%d model=%s", port, model_name)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        worker_manager.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
