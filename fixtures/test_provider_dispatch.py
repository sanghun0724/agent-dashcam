#!/usr/bin/env python3
"""Tests for provider_dispatch + agent_dashcam_score --provider flag.

Covers:
  - detect_provider path heuristics (claude/codex/gemini/fallback)
  - detect_provider first-line JSON shape sniff
  - resolve_adapter with explicit and auto modes
  - score_jsonl end-to-end through each provider path
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

AGENT_DASHCAM_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("AGENT_DASHCAM_ROOT", str(AGENT_DASHCAM_ROOT))
sys.path.insert(0, str(AGENT_DASHCAM_ROOT / "scripts"))

import agent_dashcam_score  # noqa: E402
from provider_dispatch import detect_provider, resolve_adapter  # noqa: E402


def _write(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _codex_session(model: str = "gpt-5-codex") -> list[dict]:
    return [
        {"timestamp": "2026-04-19T00:00:00.000Z", "type": "session_meta",
         "payload": {"id": "aaaa-bbbb", "timestamp": "2026-04-19T00:00:00.000Z",
                     "cwd": "/tmp/proj", "originator": "codex_cli",
                     "model": model, "instructions": None}},
        {"timestamp": "2026-04-19T00:00:01.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "read README"}]}},
        {"timestamp": "2026-04-19T00:00:02.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "reading"}]}},
        {"timestamp": "2026-04-19T00:00:03.000Z", "type": "response_item",
         "payload": {"type": "function_call", "call_id": "c1",
                     "name": "read_file", "arguments": "{\"path\": \"README.md\"}"}},
        {"timestamp": "2026-04-19T00:00:04.000Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": {"input_tokens": 1000, "output_tokens": 100,
                                                    "cached_input_tokens": 50}}}},
    ]


def _gemini_session() -> list[dict]:
    return [
        {"role": "user", "parts": [{"text": "read it"}],
         "metadata": {"timestamp": "2026-04-19T00:00:00Z", "cwd": "/tmp/proj"}},
        {"role": "model", "parts": [
            {"text": "ok"},
            {"functionCall": {"name": "read_file", "args": {"path": "README.md"}}},
        ], "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20,
                              "totalTokenCount": 120, "cachedContentTokenCount": 50},
         "metadata": {"timestamp": "2026-04-19T00:00:01Z", "model": "gemini-2.5-pro"}},
        {"role": "model", "parts": [
            {"functionResponse": {"name": "read_file", "response": {"output": "# README\n"}}},
        ], "metadata": {"timestamp": "2026-04-19T00:00:02Z"}},
    ]


def _claude_session() -> list[dict]:
    return [
        {"type": "user", "sessionId": "cl-1",
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "sessionId": "cl-1",
         "message": {"role": "assistant", "model": "claude-sonnet-4-6",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                     "content": [{"type": "text", "text": "hello"},
                                 {"type": "tool_use", "id": "t1", "name": "Read",
                                  "input": {"file_path": "README.md"}}]}},
    ]


class TestPathDetection(unittest.TestCase):
    def test_codex_path_pattern(self):
        p = Path("/tmp/.codex/sessions/2026/04/19/rollout-abc.jsonl")
        self.assertEqual(detect_provider(p), "codex")

    def test_gemini_path_pattern(self):
        p = Path("/tmp/.gemini/tmp/hash/chats/session-abc.json")
        self.assertEqual(detect_provider(p), "gemini")

    def test_claude_path_pattern(self):
        p = Path("/tmp/.claude/projects/-foo-bar/aaa.jsonl")
        self.assertEqual(detect_provider(p), "claude")

    def test_gemini_filename_only(self):
        p = Path("/tmp/elsewhere/session-11111111-2222-3333-4444-555555555555.json")
        self.assertEqual(detect_provider(p), "gemini")

    def test_codex_filename_only(self):
        p = Path("/tmp/elsewhere/rollout-abc.jsonl")
        self.assertEqual(detect_provider(p), "codex")


class TestFirstLineSniff(unittest.TestCase):
    def test_codex_session_meta_sniff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "anon.jsonl"
            _write(path, _codex_session())
            self.assertEqual(detect_provider(path), "codex")

    def test_gemini_role_parts_sniff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "anon.json"
            _write(path, _gemini_session())
            self.assertEqual(detect_provider(path), "gemini")

    def test_claude_type_user_sniff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "anon.jsonl"
            _write(path, _claude_session())
            self.assertEqual(detect_provider(path), "claude")

    def test_fallback_to_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weird.jsonl"
            _write(path, [{"foo": "bar"}])
            self.assertEqual(detect_provider(path), "claude")

    def test_missing_file_falls_back(self):
        p = Path("/tmp/definitely-not-a-real-path-agent-dashcam.jsonl")
        self.assertEqual(detect_provider(p), "claude")


class TestResolveAdapter(unittest.TestCase):
    def test_explicit_each(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.jsonl"
            _write(p, _codex_session())
            for name in ("claude", "codex", "gemini"):
                provider, adapter = resolve_adapter(name, p)
                self.assertEqual(provider, name)
                self.assertTrue(hasattr(adapter, "load_session"))

    def test_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "auto.jsonl"
            _write(p, _codex_session())
            provider, _ = resolve_adapter("auto", p)
            self.assertEqual(provider, "codex")

    def test_none_triggers_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "auto.jsonl"
            _write(p, _gemini_session())
            provider, _ = resolve_adapter(None, p)
            self.assertEqual(provider, "gemini")

    def test_unknown_provider_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.jsonl"
            _write(p, _claude_session())
            with self.assertRaises(ValueError):
                resolve_adapter("aider", p)


class TestScoreJsonl(unittest.TestCase):
    def _load_config(self):
        with open(AGENT_DASHCAM_ROOT / "config.example.json") as f:
            return json.load(f)

    def test_auto_codex_non_neutral(self):
        config = self._load_config()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "codex.jsonl"
            _write(p, _codex_session())
            result = agent_dashcam_score.score_jsonl(p, config, provider="auto")
            self.assertGreater(result["axes"]["role_focus"], 0.0)

    def test_auto_gemini_non_neutral(self):
        config = self._load_config()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "session-auto.json"
            _write(p, _gemini_session())
            result = agent_dashcam_score.score_jsonl(p, config, provider="auto")
            self.assertGreater(result["axes"]["role_focus"], 0.0)

    def test_explicit_claude_still_works(self):
        config = self._load_config()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cl.jsonl"
            _write(p, _claude_session())
            result = agent_dashcam_score.score_jsonl(p, config, provider="claude")
            self.assertGreater(result["axes"]["role_focus"], 0.0)

    def test_cli_runs_with_provider_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "codex.jsonl"
            _write(p, _codex_session())
            env = dict(os.environ)
            env["AGENT_DASHCAM_ROOT"] = str(AGENT_DASHCAM_ROOT)
            out = subprocess.run(
                [sys.executable, str(AGENT_DASHCAM_ROOT / "scripts" / "agent_dashcam_score.py"),
                 "--input", str(p), "--provider", "codex"],
                check=True, capture_output=True, text=True, env=env,
            )
            payload = json.loads(out.stdout)
            self.assertIn("axes", payload)
            self.assertGreater(payload["axes"]["role_focus"], 0.0)


if __name__ == "__main__":
    unittest.main()
