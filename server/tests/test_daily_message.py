import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import httpx

from app.daily_message import DailyMessageService, FALLBACKS, safe_text
from app.storage import Storage


class DailyMessageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name))
        self.service = DailyMessageService(self.storage)
        self.env = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'fake', 'DAILY_MESSAGE_AI_ENABLED': '1'})
        self.env.start()
        self.now = datetime(2026, 9, 27, 16, 1, tzinfo=timezone.utc)

    async def asyncTearDown(self):
        await self.service.stop()
        self.env.stop()
        self.storage.db.close()
        self.temp.cleanup()

    async def test_nonblocking_and_one_attempt_even_across_service_restart(self):
        gate = asyncio.Event()
        calls = []
        async def request(*args):
            calls.append(args)
            await gate.wait()
            return '从容出发，稳步前行'
        self.service._request = request
        first = self.service.get('a', 480, self.now)
        self.assertEqual(first['date'], '2026-09-28')
        await asyncio.sleep(0)
        for _ in range(20):
            self.assertEqual(self.service.get('a', 480, self.now), first)
        other = DailyMessageService(self.storage)
        other._request = AsyncMock(side_effect=AssertionError('duplicate paid call'))
        self.assertEqual(other.get('a', 480, self.now), first)
        gate.set()
        await asyncio.gather(*list(self.service.tasks))
        self.assertEqual(len(calls), 1)
        self.assertEqual(other.get('a', 480, self.now)['text'], '从容出发，稳步前行')
        other._request.assert_not_called()

    async def test_owner_and_date_isolation(self):
        self.service._request = AsyncMock(return_value='从容出发')
        self.service.get('a', 480, self.now)
        self.service.get('b', 480, self.now)
        self.service.get('a', 0, self.now)
        await asyncio.gather(*list(self.service.tasks))
        self.assertEqual(self.service._request.await_count, 3)
        self.assertEqual(len(self.storage.db.query('SELECT * FROM device_daily_messages')), 3)

    async def test_failure_does_not_repeat_or_drop_fallback(self):
        self.service._request = AsyncMock(side_effect=TimeoutError())
        before = self.service.get('a', 480, self.now)
        await asyncio.gather(*list(self.service.tasks))
        self.assertEqual(self.service.get('a', 480, self.now), before)
        self.assertEqual(self.service._request.await_count, 1)

    async def test_cancelled_attempt_kept_after_restart(self):
        self.service._request = AsyncMock(side_effect=lambda *args: None)
        async def slow(*args):
            await asyncio.sleep(60)
        self.service._request = slow
        before = self.service.get('a', 480, self.now)
        await asyncio.sleep(0)
        await self.service.stop()
        other = DailyMessageService(self.storage)
        other._request = AsyncMock()
        self.assertEqual(other.get('a', 480, self.now), before)
        other._request.assert_not_called()

    async def test_short_generation_disables_thinking(self):
        response = httpx.Response(200, request=httpx.Request('POST', 'https://example.test'),
            json={'choices': [{'finish_reason': 'stop', 'message': {'content': '从容出发'}}]})
        with patch('httpx.AsyncClient.post', new_callable=AsyncMock, return_value=response) as post:
            self.assertEqual(await self.service._request('2026-09-27', 6, 'fake', 'test'), '从容出发')
            self.assertEqual(post.call_args.kwargs['json']['thinking'], {'type': 'disabled'})
            self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 128)

    def test_validation(self):
        for text in FALLBACKS:
            self.assertEqual(safe_text(text), text)
        for text in ('', '长' * 13, '你好\n世界', '早上好😀', '你好\x00', '加油☀', '好\ufe0f'):
            with self.assertRaises(ValueError):
                safe_text(text)
