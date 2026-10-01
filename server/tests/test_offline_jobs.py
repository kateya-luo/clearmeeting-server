import asyncio
import tempfile
import unittest
from pathlib import Path

from app.offline_jobs import OfflineJobQueue
from app.funasr_offline_client import OfflineDependencyError, OfflinePermanentError
from app.device_offline_pipeline import ready_asr_windows
from app.segments import Segment, Timeline
from app.storage import Storage


class FailOnceOffline:
    def __init__(self, events):
        self.events = events
        self.calls = 0

    async def transcribe(self, pcm, base_offset_ms=0):
        self.calls += 1
        self.events.append(f"transcribe-{self.calls}")
        if self.calls == 1:
            raise RuntimeError("temporary outage")
        return [Segment(start_ms=base_offset_ms, end_ms=base_offset_ms + 1000, text="补回内容")]


class AlwaysFailOffline:
    async def transcribe(self, _pcm, base_offset_ms=0, *, job_id=None):
        raise RuntimeError(f"permanent outage at {base_offset_ms}")


class ParallelOffline:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.calls = []

    async def transcribe(self, _pcm, base_offset_ms=0, *, job_id=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append(base_offset_ms)
        await asyncio.sleep(0.03 if base_offset_ms == 0 else 0.01)
        self.active -= 1
        return [Segment(start_ms=base_offset_ms, end_ms=base_offset_ms + 1000,
                        text=f"slice-{base_offset_ms}")]

    async def finalize(self, session_id, canonical_sha256=None, *, job_id=None):
        self.calls.append(("canonical", session_id, canonical_sha256))
        return {"segments": [{"text": "canonical"}], "speaker_count": 2}


class NoSpeechOffline:
    async def finalize(self, session_id, canonical_sha256=None, *, job_id=None):
        return {"outcome": "no_speech", "segments": [], "audio_duration_ms": 9640}


class OfflineJobOrderingTest(unittest.TestCase):
    def test_no_speech_canonical_completes_without_replacing_existing_transcript(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as d:
                storage = Storage(Path(d))
                replaced = []

                async def on_applied(*_args):
                    pass

                async def on_canonical(*_args):
                    replaced.append(True)

                queue = OfflineJobQueue(
                    storage.root / "audio_cache", lambda _sid: Timeline(), on_applied,
                    offline=NoSpeechOffline(), on_canonical=on_canonical,
                    db=storage.db)
                await queue.enqueue("silent", 0, 0, "canonical")
                job = await queue._claim("worker")
                await queue._process(job)
                self.assertEqual(replaced, [])
                storage.db.close()

        asyncio.run(scenario())

    def test_permanent_failure_skips_all_retries(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as d:
                storage = Storage(Path(d))
                events = []

                async def on_applied(*_args):
                    pass

                async def on_give_up(job, exc):
                    events.append((job.attempts, type(exc).__name__))

                queue = OfflineJobQueue(
                    storage.root / "audio_cache", lambda _sid: Timeline(), on_applied,
                    on_give_up=on_give_up, db=storage.db)
                await queue.enqueue("bad-data", 0, 1000, "bulk")
                job = await queue._claim("worker")
                await queue._persistent_failure(
                    job, OfflinePermanentError("invalid audio"))
                row = storage.db.query_one(
                    "SELECT state,attempts FROM offline_asr_jobs WHERE id=?", (job.job_id,))
                self.assertEqual(row["state"], "failed")
                self.assertEqual(row["attempts"], 6)
                self.assertEqual(events, [(6, "OfflinePermanentError")])
                storage.db.close()

        asyncio.run(scenario())

    def test_cancelled_running_job_is_not_resurrected_by_late_failure(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as d:
                storage = Storage(Path(d))

                async def on_applied(*_args):
                    pass

                queue = OfflineJobQueue(
                    storage.root / "audio_cache", lambda _sid: Timeline(), on_applied,
                    offline=AlwaysFailOffline(), db=storage.db, worker_count=1)
                await queue.enqueue("cancel-me", 0, 1000, "bulk")
                job = await queue._claim("test-worker")
                self.assertIsNotNone(job)
                storage.db.execute(
                    "UPDATE offline_asr_jobs SET state='cancelled' WHERE id=?", (job.job_id,))
                await queue._persistent_failure(job, RuntimeError("late dependency failure"))
                row = storage.db.query_one(
                    "SELECT state,attempts FROM offline_asr_jobs WHERE id=?", (job.job_id,))
                self.assertEqual(row["state"], "cancelled")
                self.assertEqual(row["attempts"], 0)
                storage.db.close()

        asyncio.run(scenario())

    def test_canonical_waits_for_every_slice_and_summary_waits_for_canonical(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as d:
                storage = Storage(Path(d))
                audio_root = storage.root / "audio_cache"
                audio_root.mkdir(parents=True, exist_ok=True)
                (audio_root / "ordered.b.pcm.part").write_bytes(b"\0" * 96000)
                timeline = Timeline()
                recognizer = ParallelOffline()
                events = []
                finished = asyncio.Event()

                async def on_applied(_sid, _timeline, patch, _reason):
                    events.append(("slice", patch.added[0].start_ms))

                async def on_canonical(_sid, payload):
                    self.assertEqual(payload["speaker_count"], 2)
                    events.append(("canonical", None))

                async def on_summarize(_sid):
                    events.append(("summary", None))
                    finished.set()

                queue = OfflineJobQueue(
                    audio_root, lambda _sid: timeline, on_applied,
                    offline=recognizer, on_canonical=on_canonical,
                    on_summarize=on_summarize, db=storage.db, worker_count=3)
                for index in range(3):
                    await queue.enqueue("ordered", index * 1000, (index + 1) * 1000,
                                        "bulk", order_key="same", chunk_index=index)
                await queue.enqueue("ordered", 0, 0, "canonical",
                                    order_key="same", chunk_index=998)
                await queue.enqueue("ordered", 0, 0, "summarize",
                                    order_key="same", chunk_index=999)
                queue.start()
                await asyncio.wait_for(finished.wait(), timeout=2)
                await queue.stop()

                self.assertEqual(events[-2:], [("canonical", None), ("summary", None)])
                self.assertEqual(sum(1 for item in events if item[0] == "slice"), 3)
                storage.db.close()

        asyncio.run(scenario())

    def test_persistent_queue_caps_single_model_and_holds_summary_barrier(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as d:
                storage = Storage(Path(d))
                audio_root = storage.root / "audio_cache"
                audio_root.mkdir(parents=True, exist_ok=True)
                (audio_root / "long.b.pcm.part").write_bytes(b"\0" * 96000)
                timeline = Timeline()
                recognizer = ParallelOffline()
                events = []
                summarized = asyncio.Event()

                async def on_applied(_sid, _timeline, patch, _reason):
                    events.append(("applied", patch.added[0].start_ms))

                async def on_summarize(_sid):
                    events.append(("summary", None))
                    summarized.set()

                queued = OfflineJobQueue(
                    audio_root, lambda _sid: timeline, on_applied,
                    offline=recognizer, on_summarize=on_summarize,
                    db=storage.db, worker_count=3)
                for index in range(3):
                    await queued.enqueue("long", index * 1000, (index + 1) * 1000,
                                         "bulk", order_key="2026-01-01", chunk_index=index)
                await queued.enqueue("long", 0, 0, "summarize",
                                     order_key="2026-01-01", chunk_index=999)

                # 模拟任务已写 SQLite、进程却在 worker 启动前重启。
                resumed = OfflineJobQueue(
                    audio_root, lambda _sid: timeline, on_applied,
                    offline=recognizer, on_summarize=on_summarize,
                    db=storage.db, worker_count=3)
                resumed.retry_base_seconds = 0
                resumed.start()
                await asyncio.wait_for(summarized.wait(), timeout=2)
                await resumed.stop()

                self.assertEqual(recognizer.max_active, 1)
                self.assertEqual(resumed.worker_count, 1)
                self.assertEqual(events[-1], ("summary", None))
                self.assertEqual(resumed.progress("long")["done"], 3)
                self.assertEqual(resumed.progress("long")["finalization"], "done")

                # 再次重启不会重复转写已完成切片。
                calls = len(recognizer.calls)
                again = OfflineJobQueue(
                    audio_root, lambda _sid: timeline, on_applied,
                    offline=recognizer, on_summarize=on_summarize,
                    db=storage.db, worker_count=2)
                again.start()
                await asyncio.sleep(0.05)
                await again.stop()
                self.assertEqual(len(recognizer.calls), calls)
                storage.db.close()

        asyncio.run(scenario())

    def test_round_robin_across_sessions_and_strict_order_within_session(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as d:
                storage = Storage(Path(d))
                audio_root = storage.root / "audio_cache"
                audio_root.mkdir(parents=True, exist_ok=True)
                recognizer = ParallelOffline()
                timelines = {sid: Timeline() for sid in ("a", "b", "c")}
                for sid in timelines:
                    (audio_root / f"{sid}.b.pcm.part").write_bytes(b"\0" * 64000)

                async def on_applied(*_args):
                    pass

                queue = OfflineJobQueue(
                    audio_root, timelines.get, on_applied,
                    offline=recognizer, db=storage.db, worker_count=4)
                for sid in ("a", "b", "c"):
                    for index in range(2):
                        await queue.enqueue(
                            sid, index * 1000, (index + 1) * 1000, "bulk",
                            order_key="same", chunk_index=index)

                claimed = []
                for index in range(6):
                    job = await queue._claim(f"worker-{index}")
                    self.assertIsNotNone(job)
                    claimed.append((job.session_id, job.start_ms))
                    storage.db.execute(
                        "UPDATE offline_asr_jobs SET state='done' WHERE id=?", (job.job_id,))

                self.assertEqual(claimed, [
                    ("a", 0), ("b", 0), ("c", 0),
                    ("a", 1000), ("b", 1000), ("c", 1000),
                ])
                storage.db.close()

        asyncio.run(scenario())

    def test_dependency_failure_does_not_consume_business_attempts(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as d:
                storage = Storage(Path(d))

                async def on_applied(*_args):
                    pass

                queue = OfflineJobQueue(
                    storage.root / "audio_cache", lambda _sid: Timeline(), on_applied,
                    db=storage.db)
                queue.retry_base_seconds = 0
                await queue.enqueue("dependency", 0, 1000, "bulk")
                job = await queue._claim("worker")
                await queue._persistent_failure(job, OfflineDependencyError("service busy"))
                row = storage.db.query_one(
                    "SELECT state,attempts,transient_attempts,last_error"
                    " FROM offline_asr_jobs WHERE id=?", (job.job_id,))
                self.assertEqual(row["state"], "queued")
                self.assertEqual(row["attempts"], 0)
                self.assertEqual(row["transient_attempts"], 1)
                self.assertIn("OfflineDependencyError", row["last_error"])
                storage.db.close()

        asyncio.run(scenario())

    def test_two_hour_recording_uses_five_minute_windows_and_sealed_tail(self):
        bytes_per_ms = 16000 * 2 // 1000
        two_hours_ms = 2 * 60 * 60 * 1000
        total_ms = two_hours_ms + 2 * 60 * 1000
        total_bytes = total_ms * bytes_per_ms
        covered = [(0, total_bytes)]
        before_complete = ready_asr_windows(
            total_ms=total_ms, total_bytes=total_bytes, sample_rate=16000,
            bytes_per_sample=2, window_ms=5 * 60 * 1000,
            covered=covered, sealed=False)
        after_complete = ready_asr_windows(
            total_ms=total_ms, total_bytes=total_bytes, sample_rate=16000,
            bytes_per_sample=2, window_ms=5 * 60 * 1000,
            covered=covered, sealed=True)
        self.assertEqual(len(before_complete), 24)
        self.assertEqual(len(after_complete), 25)
        self.assertEqual(after_complete[-1][1:], (two_hours_ms, total_ms))
    def test_retry_finishes_before_summary_sentinel(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / "m1.b.pcm").write_bytes(b"\0" * 32000)
                events = []
                timeline = Timeline()

                async def on_applied(*_args):
                    events.append("applied")

                async def on_summarize(_sid):
                    events.append("summarize")

                queue = OfflineJobQueue(root, lambda _sid: timeline, on_applied,
                                        offline=FailOnceOffline(events), on_summarize=on_summarize)
                queue.retry_base_seconds = 0
                queue.start()
                await queue.enqueue("m1", 0, 1000, "gap")
                await queue.enqueue("m1", 0, 0, "summarize")
                await asyncio.wait_for(queue._queue.join(), timeout=1)
                await queue.stop()

                self.assertEqual(events, ["transcribe-1", "transcribe-2", "applied", "summarize"])

        asyncio.run(scenario())

    def test_retry_exhaustion_invokes_durable_give_up_and_queue_continues(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / "m2.b.pcm").write_bytes(b"\0" * 32000)
                events = []
                timeline = Timeline()

                async def on_applied(*_args):
                    self.fail("a permanently failing recognizer must not apply a patch")

                async def on_summarize(_sid):
                    events.append("summarize")

                async def on_give_up(job, exc):
                    events.append(("give_up", job.session_id, job.reason,
                                   job.attempts, type(exc).__name__))

                queue = OfflineJobQueue(
                    root, lambda _sid: timeline, on_applied,
                    offline=AlwaysFailOffline(), on_summarize=on_summarize,
                    on_give_up=on_give_up)
                queue.retry_base_seconds = 0
                queue.max_retries = 1
                queue.start()
                await queue.enqueue("m2", 0, 1000, "bulk")
                await queue.enqueue("m2", 0, 0, "summarize")
                await asyncio.wait_for(queue._queue.join(), timeout=1)
                await queue.stop()

                self.assertEqual(events, [
                    ("give_up", "m2", "bulk", 2, "RuntimeError"), "summarize"])

        asyncio.run(scenario())

    def test_nonempty_bulk_without_pcm_is_a_terminal_retry_failure(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as d:
                events = []

                async def on_applied(*_args):
                    self.fail("missing PCM must never be applied as success")

                async def on_give_up(job, exc):
                    events.append((job.session_id, job.reason, str(exc)))

                queue = OfflineJobQueue(
                    Path(d), lambda _sid: Timeline(), on_applied,
                    on_give_up=on_give_up)
                queue.max_retries = 0
                queue.retry_base_seconds = 0
                queue.start()
                await queue.enqueue("missing-pcm", 0, 1000, "bulk")
                await asyncio.wait_for(queue._queue.join(), timeout=1)
                await queue.stop()
                self.assertEqual(events, [
                    ("missing-pcm", "bulk", "authoritative PCM is missing or empty")])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
