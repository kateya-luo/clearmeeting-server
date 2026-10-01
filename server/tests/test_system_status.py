import importlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.storage import Storage
from app.system_status import create_system_status_router


class SystemStatusTest(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.storage = Storage(Path(self.temp))
        auth.configure_auth(self.storage)
        app = FastAPI()
        app.include_router(auth.router)
        self.queue = type("QueueProbe", (), {"wake": lambda queue: setattr(
            queue, "wake_count", getattr(queue, "wake_count", 0) + 1)})()
        app.include_router(create_system_status_router(
            self.storage, server_version="2.0.8",
            server_release="clearmeeting-server-v2.0.8", offline_queue=self.queue))
        self.client = TestClient(app)
        login = self.client.post("/api/v1/auth/login", json={
            "username": "TEST1", "password": "123456"})
        self.headers = {"Authorization": f"Bearer {login.json()['token']}"}

    def tearDown(self):
        self.storage.db.close()
        importlib.reload(auth)

    def _job(self, session_id, state, reason="bulk", start_ms=0, end_ms=60000):
        now = datetime.now(timezone.utc).isoformat()
        self.storage.db.execute(
            "INSERT INTO offline_asr_jobs(session_id,start_ms,end_ms,reason,chunk_index,"
            "order_key,state,attempts,transient_attempts,available_at,created_at,updated_at)"
            " VALUES(?,?,?,?,0,?,?,0,0,?,?,?)",
            (session_id, start_ms, end_ms, reason, now, state, now, now, now))
        return int(self.storage.db.query_one("SELECT last_insert_rowid() AS id")["id"])

    @patch("app.system_status.probe_speaker_backend",
           new_callable=AsyncMock, return_value={"ready": True, "detail": "ok"})
    @patch("app.system_status._probe_url",
           new_callable=AsyncMock, return_value={"ready": True, "latency_ms": 1})
    def test_requires_auth_and_returns_resources_and_queue(self, _probe, _speaker):
        self.assertEqual(self.client.get("/api/v1/system/status").status_code, 401)
        self.storage.create_meeting("mine", owner_user_id="TEST1")
        self.storage.set_title("mine", "离线测试会议", "TEST1")
        self._job("mine", "running")
        response = self.client.get("/api/v1/system/status", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        self.assertEqual(body["version"]["server_version"], "2.0.8")
        self.assertEqual(body["offline"]["counts"]["running"], 1)
        self.assertEqual(body["offline"]["meetings"][0]["title"], "离线测试会议")
        self.assertEqual(body["offline"]["meetings"][0]["state"], "running")
        self.assertIn("meeting_created_at", body["offline"]["meetings"][0])
        self.assertGreater(body["host"]["cpu_count"], 0)
        self.assertTrue(body["services"]["offline_asr"]["ready"])

    @patch("app.system_status.probe_speaker_backend",
           new_callable=AsyncMock, return_value={"ready": True, "detail": "ok"})
    @patch("app.system_status._probe_url",
           new_callable=AsyncMock, return_value={"ready": True, "latency_ms": 1})
    def test_recent_meetings_are_isolated_by_account(self, _probe, _speaker):
        self.storage.create_meeting("mine", owner_user_id="TEST1")
        self.storage.create_meeting("other", owner_user_id="TEST2")
        self._job("mine", "done")
        self._job("other", "failed")
        body = self.client.get("/api/v1/system/status", headers=self.headers).json()
        self.assertEqual([item["session_id"] for item in body["offline"]["meetings"]],
                         ["mine"])
        self.assertEqual(body["offline"]["counts"]["failed"], 1)

    @patch("app.system_status.probe_speaker_backend",
           new_callable=AsyncMock, return_value={"ready": True, "detail": "ok"})
    @patch("app.system_status._probe_url",
           new_callable=AsyncMock, return_value={"ready": True, "latency_ms": 1})
    def test_skip_failed_range_releases_publish_without_deleting_audio(self, _probe, _speaker):
        self.storage.create_meeting("mine", owner_user_id="TEST1")
        failed_id = self._job("mine", "failed", start_ms=8100000, end_ms=8400000)
        self._job("mine", "cancelled", reason="publish", end_ms=0)
        response = self.client.post(
            f"/api/v1/system/offline-jobs/{failed_id}/actions", headers=self.headers,
            json={"action": "skip"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["publish_released"])
        states = {row["reason"]: row["state"] for row in self.storage.db.query(
            "SELECT reason,state FROM offline_asr_jobs WHERE session_id='mine'")}
        self.assertEqual(states, {"bulk": "cancelled", "publish": "queued"})
        self.assertEqual(self.storage.get_state("mine"), "finalizing")
        self.assertEqual(self.queue.wake_count, 1)

    def test_failed_job_action_is_owner_scoped(self):
        self.storage.create_meeting("other", owner_user_id="TEST2")
        failed_id = self._job("other", "failed")
        response = self.client.post(
            f"/api/v1/system/offline-jobs/{failed_id}/actions", headers=self.headers,
            json={"action": "skip"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.storage.db.query_one(
            "SELECT state FROM offline_asr_jobs WHERE id=?", (failed_id,))["state"], "failed")


if __name__ == "__main__":
    unittest.main()
