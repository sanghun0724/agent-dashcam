#!/usr/bin/env python3
"""adapter_claude + canonical unit tests.

Verifies:
  - TOOL_FAMILY_MAP normalizes Claude/Codex/Gemini tool names identically
  - canonicalize_tool_name falls back to "other"
  - load_session returns the Claude-shaped dict the scorer expects
  - iter_events yields ordered CanonicalEvent with tokens/tool metadata
  - Edge cases: malformed JSONL lines are skipped, empty file returns empty data
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

AGENT_DASHCAM_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("AGENT_DASHCAM_ROOT", str(AGENT_DASHCAM_ROOT))
sys.path.insert(0, str(AGENT_DASHCAM_ROOT / "scripts"))

from adapters import claude as claude_adapter  # noqa: E402
from canonical import (  # noqa: E402
    CanonicalEvent,
    EDIT_LIKE_FAMILIES,
    READ_LIKE_FAMILIES,
    canonicalize_tool_name,
)


class ToolFamilyMapTests(unittest.TestCase):
    def test_claude_pascalcase(self):
        self.assertEqual(canonicalize_tool_name("Read"), "read")
        self.assertEqual(canonicalize_tool_name("Edit"), "edit")
        self.assertEqual(canonicalize_tool_name("Write"), "write")
        self.assertEqual(canonicalize_tool_name("Bash"), "bash")
        self.assertEqual(canonicalize_tool_name("TodoWrite"), "todo")

    def test_codex_snakecase(self):
        self.assertEqual(canonicalize_tool_name("apply_patch"), "edit")
        self.assertEqual(canonicalize_tool_name("local_shell_call"), "bash")
        self.assertEqual(canonicalize_tool_name("web_search_call"), "web_search")

    def test_gemini_snakecase(self):
        self.assertEqual(canonicalize_tool_name("read_file"), "read")
        self.assertEqual(canonicalize_tool_name("replace"), "edit")
        self.assertEqual(canonicalize_tool_name("write_file"), "write")
        self.assertEqual(canonicalize_tool_name("run_shell_command"), "bash")
        self.assertEqual(canonicalize_tool_name("grep_search"), "grep")

    def test_unknown_falls_back(self):
        self.assertEqual(canonicalize_tool_name("SomeRandomTool"), "other")
        self.assertEqual(canonicalize_tool_name(""), "other")
        self.assertEqual(canonicalize_tool_name(None), "other")

    def test_family_sets_consistent(self):
        self.assertIn("edit", EDIT_LIKE_FAMILIES)
        self.assertIn("write", EDIT_LIKE_FAMILIES)
        self.assertIn("read", READ_LIKE_FAMILIES)
        self.assertNotIn("edit", READ_LIKE_FAMILIES)


class LoadSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = AGENT_DASHCAM_ROOT / "fixtures" / "sample_0100.jsonl"
        cls.config = {"jsonl_tail_threshold_mb": 20, "jsonl_tail_lines": 5000}

    def test_returns_expected_keys(self):
        data = claude_adapter.load_session(self.fixture, self.config)
        expected = {
            "provider", "records", "partial",
            "assistant_msgs", "user_msgs", "progress_msgs", "system_msgs",
            "tool_names", "tool_uses_with_input",
            "assistant_text_lc", "user_text_lc",
            "session_id", "project_dir",
            "jsonl_lines", "jsonl_bytes",
        }
        self.assertTrue(expected.issubset(set(data.keys())))

    def test_provider_is_claude(self):
        data = claude_adapter.load_session(self.fixture, self.config)
        self.assertEqual(data["provider"], "claude")

    def test_counts_consistent_with_records(self):
        data = claude_adapter.load_session(self.fixture, self.config)
        self.assertEqual(data["jsonl_lines"], len(data["records"]))
        self.assertEqual(len(data["assistant_msgs"]) + len(data["user_msgs"])
                         + len(data["progress_msgs"]) + len(data["system_msgs"]),
                         sum(1 for r in data["records"]
                             if r.get("type") in ("assistant", "user", "progress", "system")))

    def test_partial_flag_false_for_small_fixture(self):
        data = claude_adapter.load_session(self.fixture, self.config)
        self.assertFalse(data["partial"])


class IterEventsTests(unittest.TestCase):
    def test_yields_canonical_events(self):
        fixture = AGENT_DASHCAM_ROOT / "fixtures" / "sample_0010.jsonl"
        events = list(claude_adapter.iter_events(fixture))
        self.assertGreater(len(events), 0)
        for ev in events:
            self.assertIsInstance(ev, CanonicalEvent)
            self.assertEqual(ev.provider, "claude")
            self.assertIn(ev.kind, {
                "user_message", "agent_message",
                "tool_call", "tool_result",
                "system", "progress",
            })

    def test_tool_call_has_family(self):
        fixture = AGENT_DASHCAM_ROOT / "fixtures" / "sample_0100.jsonl"
        tool_calls = [e for e in claude_adapter.iter_events(fixture)
                      if e.kind == "tool_call"]
        self.assertGreater(len(tool_calls), 0, "fixture should have at least one tool_call")
        for tc in tool_calls:
            self.assertIsNotNone(tc.tool_name)
            self.assertIsNotNone(tc.tool_family)

    def test_agent_message_carries_tokens_when_present(self):
        fixture = AGENT_DASHCAM_ROOT / "fixtures" / "sample_0100.jsonl"
        agent_msgs = [e for e in claude_adapter.iter_events(fixture)
                      if e.kind == "agent_message"]
        self.assertGreater(len(agent_msgs), 0)
        any_tokens = any(ev.tokens_output is not None for ev in agent_msgs)
        self.assertTrue(any_tokens, "at least one agent_message should report tokens")


class EdgeCaseTests(unittest.TestCase):
    def test_malformed_lines_are_skipped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write('{"type":"user","message":{"content":"hi"}}\n')
            f.write('not-json-at-all\n')
            f.write('{"type":"assistant","message":{"content":[],"usage":{"input_tokens":1,"output_tokens":1}}}\n')
            tmp = Path(f.name)
        try:
            data = claude_adapter.load_session(tmp, {})
            self.assertEqual(data["jsonl_lines"], 2)
            self.assertEqual(len(data["user_msgs"]), 1)
            self.assertEqual(len(data["assistant_msgs"]), 1)
        finally:
            tmp.unlink()

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            tmp = Path(f.name)
        try:
            data = claude_adapter.load_session(tmp, {})
            self.assertEqual(data["jsonl_lines"], 0)
            self.assertEqual(data["records"], [])
            self.assertIsNone(data["session_id"])
            self.assertIsNone(data["project_dir"])
        finally:
            tmp.unlink()

    def test_to_dict_drops_none(self):
        ev = CanonicalEvent(
            ts="2026-04-19T00:00:00Z",
            session_id="s",
            provider="claude",
            kind="user_message",
            role="user",
            text="hello",
        )
        d = ev.to_dict()
        self.assertEqual(d["kind"], "user_message")
        self.assertEqual(d["text"], "hello")
        self.assertNotIn("tokens_input", d)
        self.assertNotIn("model", d)


if __name__ == "__main__":
    unittest.main()
