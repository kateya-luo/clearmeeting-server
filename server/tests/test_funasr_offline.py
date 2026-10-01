import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.funasr_offline_client import (
    FunASROfflineClient,
    OfflineDependencyError,
    OfflinePermanentError,
    OfflineServiceBusy,
    parse_timestamp,
)


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"segments": []}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://offline/transcribe")
            raise httpx.HTTPStatusError("failed", request=request, response=httpx.Response(
                self.status_code, request=request))

    def json(self):
        return self._payload


class _Client:
    response = _Response()
    seen_headers = None
    raised = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, _url, **kwargs):
        type(self).seen_headers = kwargs.get("headers")
        if type(self).raised:
            raise type(self).raised
        return type(self).response


class ParseTimestampTest(unittest.TestCase):
    def tearDown(self):
        _Client.response = _Response()
        _Client.seen_headers = None
        _Client.raised = None

    def test_list_form(self):
        self.assertEqual(parse_timestamp([[480, 600], [600, 840]]), (480, 840))

    def test_json_string_form(self):
        self.assertEqual(parse_timestamp("[[480,600],[600,840]]"), (480, 840))

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_timestamp(None))
        self.assertIsNone(parse_timestamp(""))
        self.assertIsNone(parse_timestamp("not-json"))
        self.assertIsNone(parse_timestamp([]))

    def test_mock_transcribe_offsets(self):
        os.environ["ASR_MODE"] = "mock"
        pcm = b"\x00\x00" * 16000  # 1s @16k/16bit
        segs = asyncio.run(FunASROfflineClient().transcribe(pcm, base_offset_ms=900000))
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].start_ms, 900000)
        self.assertEqual(segs[0].end_ms, 901000)
        self.assertEqual(segs[0].source, "offline")
        self.assertEqual(segs[0].state, "final")

    def test_http_transcribe_sends_stable_job_id(self):
        client = FunASROfflineClient()
        client.http_url = "http://offline"
        with patch("app.funasr_offline_client.httpx.AsyncClient", _Client):
            asyncio.run(client._transcribe_http(
                b"\0\0", 0, job_id="offline-42"))
        self.assertEqual(_Client.seen_headers["X-Offline-Job-Id"], "offline-42")

    def test_http_transcribe_chunks_large_gap_before_request_limit(self):
        client = FunASROfflineClient()
        client.http_url = "http://offline"
        client.http_chunk_seconds = 2
        pcm = b"\0\0" * (16000 * 5)  # 5 秒，切为 2s + 2s + 1s
        part = AsyncMock(side_effect=[[], [], []])
        with patch.object(client, "_transcribe_http_part", part):
            segments = asyncio.run(client._transcribe_http(
                pcm, 7400460, job_id="offline-3063"))
        self.assertEqual(segments, [])
        self.assertEqual(part.await_count, 3)
        calls = part.await_args_list
        self.assertEqual([len(call.args[0]) for call in calls], [64000, 64000, 32000])
        self.assertEqual([call.args[1] for call in calls], [7400460, 7402460, 7404460])
        self.assertEqual(
            [call.kwargs["job_id"] for call in calls],
            ["offline-3063.http-0000", "offline-3063.http-0001", "offline-3063.http-0002"])

    def test_http_transcribe_aggregates_chunk_segments_in_order(self):
        client = FunASROfflineClient()
        client.http_url = "http://offline"
        client.http_chunk_seconds = 1
        first = object()
        second = object()
        part = AsyncMock(side_effect=[[first], [second]])
        with patch.object(client, "_transcribe_http_part", part):
            segments = asyncio.run(client._transcribe_http(
                b"\0" * 64000, 1000, job_id="offline-9"))
        self.assertEqual(segments, [first, second])

    def test_busy_response_is_transient_dependency_failure(self):
        client = FunASROfflineClient()
        client.http_url = "http://offline"
        _Client.response = _Response(429, {"error": "busy"})
        with patch("app.funasr_offline_client.httpx.AsyncClient", _Client):
            with self.assertRaises(OfflineServiceBusy):
                asyncio.run(client._transcribe_http(
                    b"\0\0", 0, job_id="offline-42"))

    def test_read_timeout_is_transient_dependency_failure(self):
        client = FunASROfflineClient()
        client.http_url = "http://offline"
        _Client.raised = httpx.ReadTimeout("slow model")
        with patch("app.funasr_offline_client.httpx.AsyncClient", _Client):
            with self.assertRaises(OfflineDependencyError):
                asyncio.run(client._transcribe_http(
                    b"\0\0", 0, job_id="offline-42"))

    def test_finalize_accepts_explicit_no_speech(self):
        client = FunASROfflineClient()
        client.http_url = "http://offline"
        _Client.response = _Response(200, {
            "outcome": "no_speech", "segments": [], "audio_duration_ms": 9640,
        })
        with patch("app.funasr_offline_client.httpx.AsyncClient", _Client):
            payload = asyncio.run(client.finalize("ly-silent", job_id="offline-1"))
        self.assertEqual(payload["outcome"], "no_speech")

    def test_invalid_request_is_permanent_not_transient(self):
        client = FunASROfflineClient()
        client.http_url = "http://offline"
        _Client.response = _Response(400, {"error": "bad input"})
        with patch("app.funasr_offline_client.httpx.AsyncClient", _Client):
            with self.assertRaises(OfflinePermanentError):
                asyncio.run(client._transcribe_http(
                    b"\0\0", 0, job_id="offline-42"))


if __name__ == "__main__":
    unittest.main()
