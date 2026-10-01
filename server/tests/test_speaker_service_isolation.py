import multiprocessing as mp
import os
import unittest

import speaker_service


def fake_speaker_worker(connection, config):
    connection.send({"type": "ready", "embedding_dims": 3})
    while True:
        try:
            request = connection.recv()
        except EOFError:
            return
        if request.get("op") == "shutdown":
            return
        raw = request.get("pcm") or b""
        if raw.startswith(b"\xff"):
            os._exit(23)
        if raw.startswith(b"\xfe"):
            connection.send({"type": "inference_error", "error": "synthetic failure"})
            continue
        connection.send({"type": "result", "embedding": [0.1, 0.2, 0.3]})


class SpeakerServiceIsolationTest(unittest.TestCase):
    def make_worker(self, max_requests=20):
        return speaker_service.SpeakerWorker(
            target=fake_speaker_worker,
            context=mp.get_context("spawn"),
            max_requests=max_requests,
            startup_timeout=15,
            request_timeout=5,
        )

    def test_pcm_fingerprint_and_validation(self):
        raw = (b"\x10\x00\xf0\xff" * 800)
        speaker_service.validate_pcm(raw)
        info = speaker_service.pcm_fingerprint(raw)
        self.assertEqual(info["bytes"], 3200)
        self.assertEqual(info["samples"], 1600)
        self.assertEqual(info["duration_ms"], 100)
        self.assertEqual(info["peak"], 16)
        self.assertEqual(len(info["sha256"]), 64)
        with self.assertRaises(ValueError):
            speaker_service.validate_pcm(b"\x00\x00" * 100)
        with self.assertRaises(ValueError):
            speaker_service.validate_pcm(b"\x00" * 3201)

    def test_native_worker_crash_is_contained_and_replaced(self):
        worker = self.make_worker()
        self.addCleanup(worker.stop)
        worker.start()
        original_generation = worker.generation
        with self.assertRaises(speaker_service.SpeakerWorkerCrashed):
            worker.embed(b"\xff\x00" * 1600, "crash-request")
        status = worker.status()
        self.assertTrue(status["ready"])
        self.assertGreater(status["generation"], original_generation)
        self.assertEqual(worker.embed(b"\x01\x00" * 1600, "after-crash"),
                         [0.1, 0.2, 0.3])

    def test_worker_rotates_before_request_after_limit(self):
        worker = self.make_worker(max_requests=2)
        self.addCleanup(worker.stop)
        worker.start()
        generation = worker.generation
        raw = b"\x01\x00" * 1600
        worker.embed(raw, "one")
        worker.embed(raw, "two")
        self.assertTrue(worker.status()["recycle_pending"])
        worker.embed(raw, "three")
        self.assertGreater(worker.generation, generation)
        self.assertEqual(worker.status()["served"], 1)

    def test_python_inference_error_recycles_worker(self):
        worker = self.make_worker()
        self.addCleanup(worker.stop)
        worker.start()
        generation = worker.generation
        with self.assertRaises(speaker_service.SpeakerWorkerError):
            worker.embed(b"\xfe\x00" * 1600, "bad-request")
        self.assertGreater(worker.generation, generation)
        self.assertTrue(worker.status()["ready"])


if __name__ == "__main__":
    unittest.main()
