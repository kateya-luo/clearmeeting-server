import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


fake_finalizer = types.ModuleType("offline_canonical_finalizer")
fake_finalizer.PIPELINE_VERSION = "test-pipeline"
fake_finalizer.SAMPLE_RATE = 16000
fake_finalizer.SAMPLE_WIDTH = 2
fake_finalizer.build_model = lambda: object()
fake_finalizer.run_finalizer = lambda *args, **kwargs: {}

with patch.dict(sys.modules, {"offline_canonical_finalizer": fake_finalizer}):
    import offline_canonical_service as service


class RecordingModel:
    def __init__(self):
        self.lengths = []

    def generate(self, **kwargs):
        self.lengths.append(int(kwargs["input_len"]))
        index = len(self.lengths)
        return [{"sentence_info": [{
            "sentence": f"part-{index}", "start": 100, "end": 900,
        }]}]


class FailingModel:
    def generate(self, **kwargs):
        raise AssertionError("cached preview part unexpectedly reached the model")


class UntimestampedModel:
    def __init__(self, text):
        self.text = text

    def generate(self, **_kwargs):
        return [{"text": self.text, "sentence_info": []}]


class OfflineCanonicalServiceChunkingTests(unittest.TestCase):
    def test_preview_preserves_text_without_timestamps(self):
        old_model = service.MODEL
        old_seconds = service.PREVIEW_CHUNK_SECONDS
        try:
            service.MODEL = UntimestampedModel("这句话没有时间戳。")
            service.PREVIEW_CHUNK_SECONDS = 60
            result = service.transcribe_preview(
                b"\x01\x00" * service.SAMPLE_RATE, 12000)
            self.assertEqual(len(result["segments"]), 1)
            self.assertEqual(result["segments"][0]["text"], "这句话没有时间戳。")
            self.assertEqual(result["segments"][0]["start_ms"], 12000)
            self.assertEqual(result["segments"][0]["end_ms"], 13000)
            self.assertTrue(result["segments"][0]["timestamp_fallback"])
        finally:
            service.MODEL = old_model
            service.PREVIEW_CHUNK_SECONDS = old_seconds

    def test_preview_empty_text_is_successful_no_speech(self):
        old_model = service.MODEL
        try:
            service.MODEL = UntimestampedModel("")
            result = service.transcribe_preview(
                b"\x00\x00" * service.SAMPLE_RATE, 0)
            self.assertEqual(result["segments"], [])
        finally:
            service.MODEL = old_model

    def test_preview_is_physically_split_and_part_cache_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            old_cache = service.CACHE_ROOT
            old_seconds = service.PREVIEW_CHUNK_SECONDS
            old_model = service.MODEL
            try:
                service.CACHE_ROOT = Path(directory)
                service.PREVIEW_CHUNK_SECONDS = 2
                model = RecordingModel()
                service.MODEL = model
                raw_pcm = b"\x00\x00" * (5 * service.SAMPLE_RATE)

                first = service.transcribe_preview(
                    raw_pcm, 300000, job_id="offline-test-1")

                self.assertEqual(model.lengths, [32000, 32000, 16000])
                self.assertEqual(first["preview_chunk_seconds"], 2)
                self.assertEqual(first["preview_chunk_count"], 3)
                self.assertEqual(
                    [item["start_ms"] for item in first["segments"]],
                    [300100, 302100, 304100],
                )
                self.assertEqual(len(list(Path(directory).glob(
                    "transcribe-part-offline-test-1.part-*.json"))), 3)

                service.MODEL = FailingModel()
                resumed = service.transcribe_preview(
                    raw_pcm, 300000, job_id="offline-test-1")
                self.assertEqual(resumed["segments"], first["segments"])
            finally:
                service.CACHE_ROOT = old_cache
                service.PREVIEW_CHUNK_SECONDS = old_seconds
                service.MODEL = old_model


if __name__ == "__main__":
    unittest.main()
