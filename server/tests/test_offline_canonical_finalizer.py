import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


fake_funasr = types.ModuleType("funasr")
fake_funasr.AutoModel = object
fake_cluster = types.ModuleType("funasr.models.campplus.cluster_backend")


class _ClusterBackend:
    pass


class _SpectralCluster:
    pass


fake_cluster.ClusterBackend = _ClusterBackend
fake_cluster.SpectralCluster = _SpectralCluster

with patch.dict(sys.modules, {
    "funasr": fake_funasr,
    "funasr.models": types.ModuleType("funasr.models"),
    "funasr.models.campplus": types.ModuleType("funasr.models.campplus"),
    "funasr.models.campplus.cluster_backend": fake_cluster,
}):
    import offline_canonical_finalizer as finalizer


class _Model:
    def __init__(self, text):
        self.text = text

    def generate(self, **_kwargs):
        return [{"text": self.text, "sentence_info": []}]


class OfflineCanonicalFinalizerOutcomeTest(unittest.TestCase):
    def _audio(self, root: str) -> Path:
        path = Path(root) / "short.b.pcm"
        path.write_bytes(b"\x00\x00" * finalizer.SAMPLE_RATE)
        return path

    def test_empty_recognition_is_successful_no_speech(self):
        with tempfile.TemporaryDirectory() as directory:
            result = finalizer.run_finalizer(
                _Model(""), "ly-silent", self._audio(directory))
        self.assertEqual(result["outcome"], "no_speech")
        self.assertEqual(result["segments"], [])
        self.assertEqual(result["audio_duration_ms"], 1000)

    def test_recognized_text_without_timestamps_gets_audio_range(self):
        with tempfile.TemporaryDirectory() as directory:
            result = finalizer.run_finalizer(
                _Model("第一句。第二句。"), "ly-fallback", self._audio(directory))
        self.assertEqual(result["outcome"], "timestamp_fallback")
        self.assertEqual("".join(item["text"] for item in result["segments"]),
                         "第一句。第二句。")
        self.assertEqual(result["segments"][0]["start_ms"], 0)
        self.assertEqual(result["segments"][-1]["end_ms"], 1000)
        self.assertTrue(all(item["timestamp_fallback"] for item in result["segments"]))


if __name__ == "__main__":
    unittest.main()
