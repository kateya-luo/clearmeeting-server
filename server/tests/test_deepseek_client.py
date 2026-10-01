import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.deepseek_client import DeepSeekClient


class FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.status_code = 200
        self.headers = {"content-type": "application/json", "x-request-id": "test-request"}

    def raise_for_status(self):
        return None


class FakeAsyncClient:
    responses = []
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        type(self).calls += 1
        return FakeResponse(type(self).responses.pop(0))


class DeepSeekMinutesResponseTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_key = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = "test-key"
        self.template = {
            "id": "01", "version": 1, "name": "测试模板", "category": "测试",
            "description": "测试", "focus": "测试", "structure": "测试",
            "special_rule": "测试", "common_rules": [], "sections": ["摘要"],
        }
        FakeAsyncClient.responses = []
        FakeAsyncClient.calls = 0

    def tearDown(self):
        if self.old_key is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = self.old_key

    async def test_empty_http_body_retries_once(self):
        valid_content = json.dumps({"summary": "完成", "sections": []}, ensure_ascii=False)
        valid_body = json.dumps({
            "model": "deepseek-v4-flash", "choices": [{"finish_reason": "stop",
            "message": {"content": valid_content}}], "usage": {},
        }, ensure_ascii=False)
        FakeAsyncClient.responses = ["", valid_body]
        with patch.object(httpx, "AsyncClient", FakeAsyncClient):
            result, _metrics = await DeepSeekClient().generate_template_minutes("完整转写", self.template)
        self.assertEqual(result["summary"], "完成")
        self.assertEqual(FakeAsyncClient.calls, 2)

    async def test_two_empty_http_bodies_return_readable_error(self):
        FakeAsyncClient.responses = ["", ""]
        with patch.object(httpx, "AsyncClient", FakeAsyncClient):
            with self.assertRaisesRegex(RuntimeError, "DeepSeek 返回空响应"):
                await DeepSeekClient().generate_template_minutes("完整转写", self.template)
        self.assertEqual(FakeAsyncClient.calls, 2)

    async def test_nonempty_malformed_body_is_not_automatically_retried(self):
        FakeAsyncClient.responses = ["not-json"]
        with patch.object(httpx, "AsyncClient", FakeAsyncClient):
            with self.assertRaisesRegex(RuntimeError, "无法解析"):
                await DeepSeekClient().generate_template_minutes("完整转写", self.template)
        self.assertEqual(FakeAsyncClient.calls, 1)


if __name__ == "__main__":
    unittest.main()
