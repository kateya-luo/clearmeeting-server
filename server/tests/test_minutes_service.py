import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.meeting_memory import MeetingMemory
from app.minutes_service import MinutesCreate, MinutesService
from app.storage import Storage


class FakeMinutesLLM:
    def __init__(self):
        self.calls = 0

    async def generate_template_minutes(self, transcript, template, **kwargs):
        self.calls += 1
        self.transcript = transcript
        self.context = kwargs
        return ({
            "schema_version": 1,
            "template": {"id": template["id"], "version": 1, "name": template["name"]},
            "title": "测试会议", "summary": "这是一次完整会后纪要。",
            "sections": [{"heading": "会议摘要", "items": ["事实一"]}],
            "conclusions": {"decisions": [], "consensus": [], "tendencies": [],
                            "suggestions": [], "disagreements": [], "unresolved": []},
            "decisions": [], "action_items": [], "participants": [],
            "memory_candidates": [], "mindmap": {"title": "测试会议", "branches": []},
        }, {"model": "fake", "prompt_tokens": 10, "completion_tokens": 20,
            "cache_hit_tokens": 0, "elapsed_ms": 1, "finish_reason": "stop"})


class FailOnceMinutesLLM(FakeMinutesLLM):
    async def generate_template_minutes(self, transcript, template, **kwargs):
        if self.calls == 0:
            self.calls += 1
            raise RuntimeError("模拟空响应")
        return await super().generate_template_minutes(transcript, template, **kwargs)


class MinutesServiceTest(unittest.IsolatedAsyncioTestCase):
    def _candidate(self, mode):
        person = self.memory.create_person("TEST1", "历史人员甲", "历史负责人", ["说话人 1"])
        self.storage.db.execute(
            "INSERT INTO meeting_speaker_assignments(session_id,speaker_id,owner_user_id,person_id,display_name,role,match_mode,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("m1", "spk_01", "TEST1", person["id"], "历史人员甲", "历史负责人", mode, "2026-09-24"))
        return person

    async def test_suggested_identity_never_reaches_minutes_or_editor_as_confirmed(self):
        person = self._candidate("suggested")
        speaker = self.memory.session_speakers("m1", "TEST1")[0]
        self.assertEqual(speaker["display_name"], "说话人 1")
        self.assertIsNone(speaker["person_id"])
        self.assertFalse(speaker["identity_confirmed"])
        self.assertEqual(speaker["suggestion"]["person_id"], person["id"])
        job = self.service.create_job("m1", "TEST1", MinutesCreate(template_id="16"))
        self.assertEqual((await self._ready(job["id"]))["state"], "ready")
        payload = json.dumps(self.fake.context, ensure_ascii=False)
        self.assertNotIn("历史人员甲", payload)
        self.assertNotIn("历史负责人", payload)
        self.assertIn("[说话人 1]", self.fake.transcript)
        participants = self.storage.get_meeting("m1")["summary"]["participants"]
        self.assertEqual(participants[0]["name"], "说话人 1")
        self.assertFalse(participants[0]["identity_confirmed"])

    async def test_legacy_auto_segment_label_is_not_trusted(self):
        person = self._candidate("auto")
        self.storage.db.execute("UPDATE segments SET speaker_label=?,person_id=? WHERE session_id='m1'",
                                ("历史人员甲", person["id"]))
        self.assertNotIn("历史人员甲", self.service._snapshot("m1", "TEST1")["transcript"])

    async def test_manual_identity_is_kept_and_generic_label_is_not_a_global_alias(self):
        self.memory.enroll_voiceprint = self._empty_voice
        await self.memory.assign_speaker("m1", "TEST1", "spk_01", display_name="确认人员乙", role="评审人", remember=True)
        speaker = self.memory.minutes_speakers("m1", "TEST1")[0]
        self.assertEqual(speaker["display_name"], "确认人员乙")
        self.assertTrue(speaker["identity_confirmed"])
        self.assertIn("[确认人员乙]", self.service._snapshot("m1", "TEST1")["transcript"])
        self.assertNotIn("说话人 1", self.memory.list_people("TEST1")[0]["aliases"])
        self.storage.create_meeting("m2", owner_user_id="TEST1")
        self.storage.upsert_segment("m2", {"seg_id":"s2", "start_ms":0,"end_ms":2000,"text":"另一场", "speaker_id":"spk_01", "speaker_label":"确认人员乙", "state":"final"})
        self.assertEqual(self.memory.minutes_speakers("m2", "TEST1")[0]["display_name"], "说话人 1")

    async def _empty_voice(self, *args, **kwargs):
        return None

    async def test_high_confidence_voiceprint_is_only_a_candidate_and_preserves_manual(self):
        person = self.memory.create_person("TEST1", "声纹候选甲")
        self.storage.db.execute(
            "INSERT INTO person_voiceprints(id,person_id,owner_user_id,embedding_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("v1", person["id"], "TEST1", "[1,0]", "2026-09-24", "2026-09-24"))
        async def embeddings(*args): return [[1, 0]]
        self.memory._speaker_embeddings = embeddings
        matches = await self.memory.match_session("m1")
        self.assertEqual(matches[0]["mode"], "suggested")
        segment = self.storage.load_segments("m1")[0]
        self.assertEqual(segment["speaker_label"], "说话人 1")
        self.assertFalse(segment.get("person_id"))
        await self.memory.assign_speaker("m1", "TEST1", "spk_01", display_name="人工确认乙")
        self.assertEqual(await self.memory.match_session("m1"), [])
        self.assertEqual(self.memory.minutes_speakers("m1", "TEST1")[0]["display_name"], "人工确认乙")

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name))
        now = "2026-08-27T00:00:00+00:00"
        self.storage.db.execute(
            "INSERT INTO users(id,username,password_hash,created_at,updated_at) VALUES('TEST1','test1','x',?,?)",
            (now, now))
        self.storage.create_meeting("m1", owner_user_id="TEST1")
        self.storage.upsert_segment("m1", {"seg_id": "s1", "start_ms": 0, "end_ms": 2000,
                                                   "text": "完整转写", "speaker_id": "spk_01",
                                                   "speaker_label": "说话人 1", "state": "final"})
        self.storage.set_state("m1", "transcript_ready")
        self.memory = MeetingMemory(self.storage)
        self.service = MinutesService(self.storage, self.memory)
        self.fake = FakeMinutesLLM()
        self.service.llm = self.fake

    async def asyncTearDown(self):
        await self.service.stop()
        self.storage.db.close()
        self.temp.cleanup()

    async def _ready(self, job_id):
        for _ in range(100):
            job = self.service._job(job_id)
            if job["state"] in {"ready", "failed"}:
                return job
            await asyncio.sleep(0.01)
        self.fail("minutes job timed out")

    async def test_thirty_templates_and_double_click_is_one_model_call(self):
        self.assertEqual(len(self.service.list_templates()), 30)
        first = self.service.create_job("m1", "TEST1", MinutesCreate(template_id="16"))
        second = self.service.create_job("m1", "TEST1", MinutesCreate(template_id="16"))
        self.assertEqual(first["id"], second["id"])
        job = await self._ready(first["id"])
        self.assertEqual(job["state"], "ready")
        self.assertEqual(self.fake.calls, 1)
        meeting = self.storage.get_meeting("m1", "TEST1")
        self.assertEqual(meeting["minutes_status"], "ready")
        self.assertEqual(meeting["summary"]["template"]["id"], "16")

    async def test_no_job_means_no_model_call(self):
        await asyncio.sleep(0.02)
        self.assertEqual(self.fake.calls, 0)
        self.assertEqual(self.storage.get_meeting("m1")["minutes_status"], "not_created")

    async def test_failed_same_request_is_reset_and_requeued(self):
        self.service.llm = FailOnceMinutesLLM()
        first = self.service.create_job("m1", "TEST1", MinutesCreate(template_id="16"))
        failed = await self._ready(first["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertIsNotNone(failed["last_error"])
        await asyncio.sleep(0)

        retried = self.service.create_job("m1", "TEST1", MinutesCreate(template_id="16"))
        self.assertEqual(retried["id"], first["id"])
        self.assertEqual(retried["state"], "queued")
        self.assertIsNone(retried["last_error"])
        ready = await self._ready(first["id"])
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["attempts"], 1)
        self.assertEqual(self.service.llm.calls, 2)
        self.assertEqual(self.storage.get_meeting("m1", "TEST1")["minutes_status"], "ready")

    async def test_manual_person_memory_is_owner_scoped(self):
        async def no_voice(*args, **kwargs):
            return None
        self.memory.enroll_voiceprint = no_voice
        assignment = await self.memory.assign_speaker(
            "m1", "TEST1", "spk_01", display_name="骆洲", role="负责人", remember=True)
        self.assertEqual(assignment["display_name"], "骆洲")
        self.assertTrue(assignment["remembered"])
        people = self.memory.list_people("TEST1")
        self.assertEqual([p["display_name"] for p in people], ["骆洲"])
        self.assertEqual(self.memory.list_people("TEST2"), [])


if __name__ == "__main__":
    unittest.main()
