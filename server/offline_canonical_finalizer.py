"""CPU-only canonical ASR + speaker diarization for completed recorder sessions.

This is deliberately separate from the live path.  It consumes the verified
canonical 16 kHz / 16-bit / mono PCM file and uses FunASR's integrated
VAD -> Paraformer -> punctuation -> CAM++ -> global clustering pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from funasr import AutoModel
from funasr.models.campplus.cluster_backend import ClusterBackend, SpectralCluster


LOG = logging.getLogger("clearmeeting.offline_canonical")
PIPELINE_VERSION = "offline-canonical-diarization-v2.0.0"
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*")


class AdaptiveClusterBackend(ClusterBackend):
    """Remove the upstream short-meeting and 15-speaker hard limits.

    FunASR 1.1.12 forces every input with fewer than 20 embeddings to one
    speaker and its spectral backend only searches up to 15 speakers.  For a
    short meeting we search a dynamic evidence-bound range (at least two
    embeddings per speaker).  For longer meetings we preserve the proven
    upstream result unless it actually saturates the 15-speaker ceiling; only
    then do we widen the search.  This keeps normal meetings stable without
    imposing a participant-count setting.
    """

    def forward(self, embeddings, **params):
        if params.get("oracle_num") is not None:
            return super().forward(embeddings, **params)
        count = int(embeddings.shape[0])
        if count < 4:
            return np.zeros(count, dtype="int")
        if count < 20:
            search_max = max(2, count // 2)
            labels = SpectralCluster(
                min_num_spks=1, max_num_spks=search_max
            )(embeddings)
            return self.merge_by_cos(
                labels, embeddings, self.model_config["merge_thr"]
            )
        labels = super().forward(embeddings, **params)
        if count < 2048 and int(np.max(labels)) + 1 >= 15:
            labels = SpectralCluster(
                min_num_spks=1, max_num_spks=max(2, count // 2)
            )(embeddings)
            labels = self.merge_by_cos(
                labels, embeddings, self.model_config["merge_thr"]
            )
        return labels


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _model_path(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if value.startswith("/") and not Path(value).exists():
        raise FileNotFoundError(f"configured model path does not exist: {name}")
    return value


def build_model() -> AutoModel:
    asr_model = _model_path(
        "OFFLINE_CANONICAL_ASR_MODEL",
        "/workspace/models/iic/"
        "speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    )
    vad_model = _model_path(
        "OFFLINE_CANONICAL_VAD_MODEL",
        "/workspace/models/hub/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    )
    punc_model = _model_path(
        "OFFLINE_CANONICAL_PUNC_MODEL",
        "/workspace/models/hub/iic/punc_ct-transformer_cn-en-common-vocab471067-large",
    )
    spk_model = _model_path(
        "OFFLINE_CANONICAL_SPK_MODEL",
        "/workspace/models/hub/iic/speech_campplus_sv_zh-cn_16k-common",
    )
    merge_threshold = float(os.getenv("OFFLINE_CANONICAL_SPK_MERGE_THRESHOLD", "0.78"))
    LOG.info("loading_offline_canonical_models pipeline=%s", PIPELINE_VERSION)
    model = AutoModel(
        model=asr_model,
        vad_model=vad_model,
        punc_model=punc_model,
        spk_model=spk_model,
        spk_mode="punc_segment",
        spk_kwargs={"cb_kwargs": {"merge_thr": merge_threshold}},
        device="cpu",
        disable_update=True,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
    model.cb_model = AdaptiveClusterBackend(merge_thr=merge_threshold).to("cpu")
    return model


def load_pcm(path: Path) -> np.ndarray:
    size = path.stat().st_size
    if size <= 0 or size % SAMPLE_WIDTH:
        raise ValueError(f"canonical PCM has invalid byte length: {size}")
    mapped = np.memmap(path, mode="r", dtype="<i2")
    # FunASR expects normalized float audio.  The conversion is the only full
    # audio copy; the raw file itself remains disk-backed.
    audio = mapped.astype(np.float32)
    audio *= 1.0 / 32768.0
    return audio


def _stable_speaker_map(sentences: list[dict[str, Any]]) -> dict[int, str]:
    order: dict[int, str] = {}
    for sentence in sentences:
        raw = sentence.get("spk")
        if raw is None:
            continue
        speaker = int(raw)
        if speaker not in order:
            order[speaker] = f"spk_{len(order) + 1:02d}"
    return order


def normalize_sentences(
    session_id: str,
    sentences: list[dict[str, Any]],
    duration_ms: int,
) -> list[dict[str, Any]]:
    ordered = sorted(sentences, key=lambda item: (int(item.get("start") or 0), int(item.get("end") or 0)))
    speaker_map = _stable_speaker_map(ordered)
    output: list[dict[str, Any]] = []
    for item in ordered:
        text = str(item.get("sentence") or item.get("text") or "").strip()
        if not text:
            continue
        start_ms = min(duration_ms, max(0, int(round(float(item.get("start") or 0)))))
        end_ms = min(duration_ms, max(start_ms, int(round(float(item.get("end") or start_ms)))))
        if end_ms <= start_ms:
            continue
        raw_speaker = item.get("spk")
        speaker_id = speaker_map.get(int(raw_speaker)) if raw_speaker is not None else None
        identity = f"{session_id}|{start_ms}|{end_ms}|{text}".encode("utf-8")
        output.append({
            "seg_id": "canon-" + hashlib.sha256(identity).hexdigest()[:24],
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
            "speaker_id": speaker_id,
            "speaker_label": (
                f"说话人 {int(speaker_id.rsplit('_', 1)[1])}" if speaker_id else None
            ),
            "speaker_final": True,
            "source": "offline_canonical",
            "state": "final",
        })
    return output


def fallback_text_segments(
    session_id: str, text: str, duration_ms: int,
) -> list[dict[str, Any]]:
    """Preserve recognized text when FunASR omitted sentence timestamps.

    The fallback is deliberately deterministic: punctuation-delimited sentences
    are distributed across the real audio duration by character count.  It is
    less precise than native timestamps but never discards recognized speech.
    """
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized or duration_ms <= 0:
        return []
    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(normalized)
                 if part.strip()] or [normalized]
    total_chars = sum(len(part) for part in sentences) or 1
    output: list[dict[str, Any]] = []
    cursor = 0
    consumed_chars = 0
    for index, sentence in enumerate(sentences):
        consumed_chars += len(sentence)
        end_ms = duration_ms if index == len(sentences) - 1 else max(
            cursor + 1, round(duration_ms * consumed_chars / total_chars))
        end_ms = min(duration_ms, end_ms)
        identity = f"{session_id}|{cursor}|{end_ms}|{sentence}".encode("utf-8")
        output.append({
            "seg_id": "canon-" + hashlib.sha256(identity).hexdigest()[:24],
            "start_ms": cursor,
            "end_ms": end_ms,
            "text": sentence,
            "speaker_id": None,
            "speaker_label": None,
            "speaker_final": True,
            "source": "offline_canonical_timestamp_fallback",
            "state": "final",
            "timestamp_fallback": True,
        })
        cursor = end_ms
    return output


def run_finalizer(model: AutoModel, session_id: str, audio_path: Path) -> dict[str, Any]:
    started = time.monotonic()
    byte_count = audio_path.stat().st_size
    duration_ms = round(byte_count * 1000 / (SAMPLE_RATE * SAMPLE_WIDTH))
    audio_sha256 = _file_sha256(audio_path)
    audio = load_pcm(audio_path)
    LOG.info(
        "offline_canonical_started session=%s bytes=%d duration_ms=%d",
        session_id,
        byte_count,
        duration_ms,
    )
    results = model.generate(
        input=audio,
        input_len=len(audio),
        batch_size_s=int(os.getenv("OFFLINE_CANONICAL_BATCH_SIZE_S", "300")),
        merge_vad=False,
        sentence_timestamp=True,
        return_spk_res=True,
        use_itn=True,
    )
    raw = results[0] if results else {}
    sentence_info = raw.get("sentence_info") or []
    segments = normalize_sentences(session_id, sentence_info, duration_ms)
    recognized_text = str(raw.get("text") or "").strip()
    outcome = "ok"
    if not segments and recognized_text:
        segments = fallback_text_segments(session_id, recognized_text, duration_ms)
        outcome = "timestamp_fallback"
        LOG.warning(
            "offline_canonical_timestamp_fallback session=%s chars=%d duration_ms=%d",
            session_id, len(recognized_text), duration_ms)
    elif not segments:
        outcome = "no_speech"
        LOG.info(
            "offline_canonical_no_speech session=%s duration_ms=%d",
            session_id, duration_ms)
    speaker_counts = Counter(
        segment["speaker_id"] for segment in segments if segment.get("speaker_id")
    )
    elapsed_ms = round((time.monotonic() - started) * 1000)
    payload = {
        "pipeline_version": PIPELINE_VERSION,
        "mode": "canonical",
        "outcome": outcome,
        "session_id": session_id,
        "canonical_sha256": audio_sha256,
        "audio_bytes": byte_count,
        "audio_duration_ms": duration_ms,
        "processing_ms": elapsed_ms,
        "realtime_factor": round(elapsed_ms / max(1, duration_ms), 4),
        "segment_count": len(segments),
        "speaker_count": len(speaker_counts),
        "speaker_segment_counts": dict(sorted(speaker_counts.items())),
        "segments": segments,
    }
    LOG.info(
        "offline_canonical_done session=%s segments=%d speakers=%d processing_ms=%d rtf=%.4f",
        session_id,
        len(segments),
        len(speaker_counts),
        elapsed_ms,
        payload["realtime_factor"],
    )
    return payload


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper()),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not args.audio.is_file():
        raise FileNotFoundError(args.audio)
    model = build_model()
    payload = run_finalizer(model, args.session_id, args.audio)
    atomic_write_json(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
