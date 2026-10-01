"""Internal HTTP service for preview ASR and canonical meeting finalization."""

from __future__ import annotations

import json
import gc
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from offline_canonical_finalizer import (
    PIPELINE_VERSION,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    build_model,
    run_finalizer,
)


LOG = logging.getLogger("clearmeeting.offline_canonical_service")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
AUDIO_ROOT = Path(os.getenv("OFFLINE_CANONICAL_AUDIO_ROOT", "/app/data/audio_cache"))
PORT = int(os.getenv("OFFLINE_CANONICAL_PORT", "10096"))
MAX_PREVIEW_BYTES = int(os.getenv("OFFLINE_CANONICAL_MAX_PREVIEW_BYTES", str(20 * 1024 * 1024)))
PREVIEW_CHUNK_SECONDS = max(15, min(
    120, int(os.getenv("OFFLINE_CANONICAL_PREVIEW_CHUNK_SECONDS", "60"))))
CACHE_ROOT = Path(os.getenv(
    "OFFLINE_CANONICAL_CACHE_ROOT", "/workspace/models/clearmeeting-offline-results"))
MODEL = build_model()
MODEL_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
ACTIVE_JOB: dict[str, object] = {}
LAST_SUCCESS: dict[str, object] = {}


def _cache_path(endpoint: str, job_id: str) -> Path:
    return CACHE_ROOT / f"{endpoint}-{job_id}.json"


def _read_cached(endpoint: str, job_id: str) -> dict | None:
    path = _cache_path(endpoint, job_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


def _write_cached(endpoint: str, job_id: str, payload: dict) -> None:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    target = _cache_path(endpoint, job_id)
    temporary = target.with_suffix(f".{threading.get_ident()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(target)


def _start_job(endpoint: str, job_id: str) -> bool:
    if not MODEL_LOCK.acquire(blocking=False):
        return False
    with STATE_LOCK:
        ACTIVE_JOB.clear()
        ACTIVE_JOB.update({
            "job_id": job_id,
            "endpoint": endpoint,
            "started_monotonic": time.monotonic(),
            "started_at": time.time(),
        })
    return True


def _finish_job(endpoint: str, job_id: str, success: bool) -> None:
    with STATE_LOCK:
        if success:
            LAST_SUCCESS.clear()
            LAST_SUCCESS.update({
                "job_id": job_id, "endpoint": endpoint, "finished_at": time.time()})
        ACTIVE_JOB.clear()
    MODEL_LOCK.release()


def _update_active_job(**values: object) -> None:
    with STATE_LOCK:
        if ACTIVE_JOB:
            ACTIVE_JOB.update(values)


def _preview_segments(
    raw_result: dict, base_offset_ms: int, part_end_ms: int | None = None,
) -> list[dict]:
    output = []
    for index, item in enumerate(raw_result.get("sentence_info") or []):
        text = str(item.get("sentence") or item.get("text") or "").strip()
        if not text:
            continue
        start_ms = base_offset_ms + max(0, int(round(float(item.get("start") or 0))))
        end_ms = base_offset_ms + max(
            int(round(float(item.get("start") or 0))),
            int(round(float(item.get("end") or item.get("start") or 0))),
        )
        if end_ms <= start_ms:
            continue
        output.append({
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
            "source": "offline",
            "state": "final",
            "preview_index": index,
        })
    if output:
        return output
    text = str(raw_result.get("text") or "").strip()
    if text and part_end_ms is not None and part_end_ms > base_offset_ms:
        LOG.warning(
            "offline_preview_timestamp_fallback base=%d end=%d chars=%d",
            base_offset_ms, part_end_ms, len(text))
        return [{
            "start_ms": base_offset_ms,
            "end_ms": part_end_ms,
            "text": text,
            "source": "offline_timestamp_fallback",
            "state": "final",
            "preview_index": 0,
            "timestamp_fallback": True,
        }]
    return []


def transcribe_preview(raw_pcm: bytes, base_offset_ms: int, *, job_id: str = "") -> dict:
    if not raw_pcm or len(raw_pcm) % SAMPLE_WIDTH:
        raise ValueError("preview PCM is empty or misaligned")
    chunk_samples = PREVIEW_CHUNK_SECONDS * SAMPLE_RATE
    total_samples = len(raw_pcm) // SAMPLE_WIDTH
    total_parts = (total_samples + chunk_samples - 1) // chunk_samples
    segments: list[dict] = []
    pcm_view = memoryview(raw_pcm)
    for part_index in range(total_parts):
        sample_start = part_index * chunk_samples
        sample_end = min(total_samples, sample_start + chunk_samples)
        part_offset_ms = base_offset_ms + round(sample_start * 1000 / SAMPLE_RATE)
        part_end_ms = base_offset_ms + round(sample_end * 1000 / SAMPLE_RATE)
        part_job_id = f"{job_id}.part-{part_index:04d}" if job_id else ""
        cached = _read_cached("transcribe-part", part_job_id) if part_job_id else None
        _update_active_job(
            part_index=part_index + 1,
            part_count=total_parts,
            part_start_ms=part_offset_ms,
            part_end_ms=part_end_ms,
            part_cached=cached is not None,
        )
        if cached is not None:
            segments.extend(cached.get("segments") or [])
            continue
        byte_start = sample_start * SAMPLE_WIDTH
        byte_end = sample_end * SAMPLE_WIDTH
        audio = np.frombuffer(pcm_view[byte_start:byte_end], dtype="<i2").astype(np.float32)
        audio *= 1.0 / 32768.0
        results = None
        try:
            # V2.0.3: 每次 native 调用最多接收一个真实 60 秒 PCM 子块。
            # batch_size_s 只是 FunASR 内部参数，不能替代外层物理切分。
            results = MODEL.generate(
                input=audio,
                input_len=len(audio),
                batch_size_s=min(
                    PREVIEW_CHUNK_SECONDS,
                    int(os.getenv("OFFLINE_CANONICAL_BATCH_SIZE_S", "60")),
                ),
                merge_vad=False,
                sentence_timestamp=True,
                return_spk_res=False,
                use_itn=True,
            )
            payload = {"segments": (
                _preview_segments(results[0], part_offset_ms, part_end_ms)
                if results else [])}
            if part_job_id:
                _write_cached("transcribe-part", part_job_id, payload)
            segments.extend(payload["segments"])
        finally:
            # 尽快释放每个子块的 float32 音频和 native 返回对象，避免五个子块叠加驻留。
            del results
            del audio
            gc.collect()
    segments.sort(key=lambda item: (
        int(item.get("start_ms") or 0), int(item.get("end_ms") or 0),
        int(item.get("preview_index") or 0)))
    return {
        "segments": segments,
        "preview_chunk_seconds": PREVIEW_CHUNK_SECONDS,
        "preview_chunk_count": total_parts,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ClearMeetingOfflineCanonical/2.0.9"

    def do_GET(self):
        if urlparse(self.path).path != "/health":
            self.send_error(404)
            return
        with STATE_LOCK:
            active = dict(ACTIVE_JOB)
            last_success = dict(LAST_SUCCESS)
        started = float(active.pop("started_monotonic", 0) or 0)
        if active.get("started_at"):
            active["elapsed_seconds"] = max(0, round(time.monotonic() - started, 1))
        self.send_json(200, {
            "status": "ok",
            "pipeline_version": PIPELINE_VERSION,
            "busy": MODEL_LOCK.locked(),
            "active": active or None,
            "last_success": last_success or None,
        })

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/transcribe":
                self.handle_transcribe()
            elif path == "/finalize":
                self.handle_finalize()
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            LOG.warning("offline_canonical_client_disconnected path=%s", path)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)[:240]})
        except FileNotFoundError:
            self.send_json(404, {"error": "canonical audio not found"})
        except Exception as exc:
            LOG.exception("offline_canonical_request_failed path=%s error=%s", path, exc)
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"[:500]})

    def handle_transcribe(self):
        job_id = self.job_id()
        cached = _read_cached("transcribe", job_id) if job_id else None
        if cached is not None:
            self.send_json(200, cached)
            return
        if not _start_job("transcribe", job_id):
            self.send_json(429, self.busy_payload())
            return
        success = False
        try:
            length = self.content_length(MAX_PREVIEW_BYTES)
            raw_pcm = self.rfile.read(length)
            if len(raw_pcm) != length:
                raise ValueError("incomplete preview request body")
            base_offset_ms = max(0, int(self.headers.get("X-Base-Offset-Ms", "0")))
            payload = transcribe_preview(raw_pcm, base_offset_ms, job_id=job_id)
            if job_id:
                _write_cached("transcribe", job_id, payload)
            success = True
            self.send_json(200, payload)
        finally:
            _finish_job("transcribe", job_id, success)

    def handle_finalize(self):
        job_id = self.job_id()
        cached = _read_cached("finalize", job_id) if job_id else None
        if cached is not None:
            self.send_json(200, cached)
            return
        if not _start_job("finalize", job_id):
            self.send_json(429, self.busy_payload())
            return
        success = False
        try:
            length = self.content_length(64 * 1024)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            session_id = str(body.get("session_id") or "")
            if not SESSION_ID.fullmatch(session_id):
                raise ValueError("invalid session id")
            audio_path = AUDIO_ROOT / f"{session_id}.b.pcm"
            payload = run_finalizer(MODEL, session_id, audio_path)
            expected_sha256 = str(body.get("canonical_sha256") or "").lower()
            if expected_sha256 and payload["canonical_sha256"] != expected_sha256:
                raise ValueError("canonical SHA-256 mismatch")
            if job_id:
                _write_cached("finalize", job_id, payload)
            success = True
            self.send_json(200, payload)
        finally:
            _finish_job("finalize", job_id, success)

    def job_id(self) -> str:
        value = str(self.headers.get("X-Offline-Job-Id") or "").strip()
        if value and not JOB_ID.fullmatch(value):
            raise ValueError("invalid offline job id")
        return value

    @staticmethod
    def busy_payload() -> dict:
        with STATE_LOCK:
            active = dict(ACTIVE_JOB)
        active.pop("started_monotonic", None)
        return {"error": "offline model busy", "active": active or None}

    def content_length(self, maximum: int) -> int:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > maximum:
            raise ValueError("invalid Content-Length")
        return length

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        LOG.info("request " + fmt, *args)


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper()),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    LOG.info("offline_canonical_service_ready port=%d pipeline=%s", PORT, PIPELINE_VERSION)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
