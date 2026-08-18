"""LLM 层测试：所有文案生成都通过 dlazy 的单一文本工具。

上游按 21 家 Provider 分支，测试也随之覆盖每家的鉴权、Base URL 和响应解析。
模型层收敛到 dlazy 之后，这些分支不复存在，这里只验证真正还在的契约：
payload 形状、响应解析、错误返回约定，以及上层解析逻辑不受影响。
"""

import unittest
from unittest.mock import patch

from app.config import config
from app.services import llm


class TestGenerateResponse(unittest.TestCase):
    def setUp(self):
        self._orig_model = config.dlazy.get("llm_model")
        config.dlazy["llm_model"] = "claude-sonnet-5"

    def tearDown(self):
        if self._orig_model is None:
            config.dlazy.pop("llm_model", None)
        else:
            config.dlazy["llm_model"] = self._orig_model

    def test_sends_full_payload_and_returns_text(self):
        """dlazy 文本工具把四个字段都列为必填，payload 必须发全。"""
        with patch.object(
            llm.dlazy_client, "run_tool", return_value={"texts": ["hello"]}
        ) as run_tool:
            result = llm._generate_response("write something")

        self.assertEqual(result, "hello")
        model, payload = run_tool.call_args[0]
        self.assertEqual(model, "claude-sonnet-5")
        self.assertEqual(payload["prompt"], "write something")
        for field in ("images", "videos", "promptRefs"):
            self.assertEqual(payload[field], [])

    def test_honours_runtime_config_snapshot(self):
        """生成期间切换模型不应影响进行中的请求，快照优先于全局配置。"""
        with patch.object(
            llm.dlazy_client, "run_tool", return_value={"texts": ["ok"]}
        ) as run_tool:
            llm._generate_response("x", app_config={"dlazy_llm_model": "kimi-k3"})

        self.assertEqual(run_tool.call_args[0][0], "kimi-k3")

    def test_empty_response_is_reported_as_error_string(self):
        """调用方靠 "Error:" 前缀判错，不能改成抛异常。"""
        with patch.object(llm.dlazy_client, "run_tool", return_value={"texts": []}):
            result = llm._generate_response("x")
        self.assertTrue(result.startswith("Error:"))

    def test_tool_failure_is_reported_as_error_string(self):
        with patch.object(
            llm.dlazy_client, "run_tool", side_effect=RuntimeError("boom")
        ):
            result = llm._generate_response("x")
        self.assertTrue(result.startswith("Error:"))
        self.assertIn("boom", result)


class TestGenerateTerms(unittest.TestCase):
    """dlazy 没有 response_format:json_object，JSON 全靠文本解析兜底。"""

    def test_parses_json_wrapped_in_code_fence(self):
        fenced = '```json\n["city skyline", "night traffic"]\n```'
        with patch.object(llm, "_generate_response", return_value=fenced):
            terms = llm.generate_terms("city at night", "a short script", amount=2)
        self.assertEqual(terms, ["city skyline", "night traffic"])

    def test_error_response_yields_no_terms(self):
        with patch.object(llm, "_generate_response", return_value="Error: nope"):
            terms = llm.generate_terms("subject", "script", amount=2)
        self.assertEqual(terms, [])


if __name__ == "__main__":
    unittest.main()
