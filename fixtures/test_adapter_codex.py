#!/usr/bin/env python3
"""adapter_codex unit tests.

Verifies:
  - TOOL_FAMILY_MAP already normalizes Codex tool names to the right families.
  - load_session returns the Claude-shaped dict the scorer expects.
  - Response item types (message/function_call/local_shell_call/web_search_call/
    reasoning) project into correct Claude content blocks.
  - JSON-string `arguments` parsing is defensive (valid JSON, invalid JSON, empty).
  - function_call_output becomes a user tool_result with the right call_id.
  - token_count event_msg attaches usage to the most recent assistant msg.
  - iter_events yields CanonicalEvent with tool_family populated.
  - Malformed lines / empty files do not crash.
  - The full adapter output is consumable by agent_dashcam_score.score_jsonl.
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

from adapters import codex as codex_adapter  # noqa: E402
from canonical import CanonicalEvent, canonicalize_tool_name  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _write_jsonl(records: list[dict]) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for r in records:
        tmp.write(json.dumps(r) + "\n")
    tmp.close()
    return Path(tmp.name)


def _session_meta(
    session_id: str = "11111111-1111-1111-1111-111111111111",
    cwd: str = "/Users/fixture/project",
    model: str = "gpt-5-codex",
) -> dict:
    return {
        "timestamp": "2026-04-19T00:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "timestamp": "2026-04-19T00:00:00.000Z",
            "cwd": cwd,
            "originator": "codex_cli",
            "model": model,
            "instructions": None,
        },
    }


def _user_msg(text: str) -> dict:
    return {
        "timestamp": "2026-04-19T00:00:01.000Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _function_call(name: str, args, call_id: str = "fc_1") -> dict:
    if not isinstance(args, str):
        args = json.dumps(args)
    return {
        "timestamp": "2026-04-19T00:00:02.000Z",
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": name,
            "arguments": args,
            "call_id": call_id,
        },
    }


def _local_shell_call(command: list[str], call_id: str = "lsc_1") -> dict:
    return {
        "timestamp": "2026-04-19T00:00:03.000Z",
        "type": "response_item",
        "payload": {
            "type": "local_shell_call",
            "action": {"command": command},
            "call_id": call_id,
        },
    }


def _web_search_call(query: str, call_id: str = "ws_1") -> dict:
    return {
        "timestamp": "2026-04-19T00:00:04.000Z",
        "type": "response_item",
        "payload": {
            "type": "web_search_call",
            "query": query,
            "call_id": call_id,
        },
    }


def _reasoning(text: str) -> dict:
    return {
        "timestamp": "2026-04-19T00:00:05.000Z",
        "type": "response_item",
        "payload": {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": text}],
        },
    }


def _function_call_output(call_id: str, output: str) -> dict:
    return {
        "timestamp": "2026-04-19T00:00:06.000Z",
        "type": "function_call_output",
        "payload": {"call_id": call_id, "output": output},
    }


def _token_count(input_tokens=1000, cached=800, output=250) -> dict:
    return {
        "timestamp": "2026-04-19T00:00:07.000Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": 0,
                    "total_tokens": input_tokens + output,
                },
            },
        },
    }


def _assistant_message(text: str) -> dict:
    return {
        "timestamp": "2026-04-19T00:00:08.000Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class ToolFamilyCodexNamesTests(unittest.TestCase):
    """Canonical map already handles Codex names — this is a sanity belt."""

    def test_codex_tool_family_mapping(self):
        self.assertEqual(canonicalize_tool_name("read_file"), "read")
        self.assertEqual(canonicalize_tool_name("apply_patch"), "edit")
        self.assertEqual(canonicalize_tool_name("local_shell_call"), "bash")
        self.assertEqual(canonicalize_tool_name("web_search_call"), "web_search")
        self.assertEqual(canonicalize_tool_name("write_file"), "write")


class LoadSessionShapeTests(unittest.TestCase):
    """load_session must expose the same 15 keys as the Claude adapter."""

    @classmethod
    def setUpClass(cls):
        cls.path = _write_jsonl([
            _session_meta(),
            _user_msg("please read src/app.py"),
            _reasoning("I should read the file first."),
            _function_call("read_file", {"path": "src/app.py"}, call_id="fc_1"),
            _token_count(),
            _function_call_output("fc_1", "contents..."),
        ])

    @classmethod
    def tearDownClass(cls):
        try:
            cls.path.unlink()
        except FileNotFoundError:
            pass

    def test_returns_required_keys(self):
        data = codex_adapter.load_session(self.path, {})
        expected = {
            "provider", "records", "partial",
            "assistant_msgs", "user_msgs", "progress_msgs", "system_msgs",
            "tool_names", "tool_uses_with_input",
            "assistant_text_lc", "user_text_lc",
            "session_id", "project_dir",
            "jsonl_lines", "jsonl_bytes",
        }
        self.assertTrue(expected.issubset(set(data.keys())))

    def test_provider_is_codex(self):
        data = codex_adapter.load_session(self.path, {})
        self.assertEqual(data["provider"], "codex")

    def test_session_meta_extracted(self):
        data = codex_adapter.load_session(self.path, {})
        self.assertEqual(data["session_id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(data["project_dir"], "/Users/fixture/project")

    def test_partial_flag_false_for_small_fixture(self):
        data = codex_adapter.load_session(self.path, {})
        self.assertFalse(data["partial"])
        self.assertGreater(data["jsonl_bytes"], 0)


class FunctionCallProjectionTests(unittest.TestCase):
    def test_function_call_becomes_tool_use(self):
        path = _write_jsonl([
            _session_meta(),
            _function_call("read_file", {"path": "a.py"}, call_id="fc_a"),
        ])
        try:
            data = codex_adapter.load_session(path, {})
            self.assertEqual(data["tool_names"], ["read_file"])
            name, inp = data["tool_uses_with_input"][0]
            self.assertEqual(name, "read_file")
            self.assertEqual(inp, {"path": "a.py"})
        finally:
            path.unlink()

    def test_arguments_json_string_is_parsed(self):
        path = _write_jsonl([
            _session_meta(),
            _function_call("apply_patch", json.dumps({"patch": "diff --git"}), call_id="fc_b"),
        ])
        try:
            data = codex_adapter.load_session(path, {})
            self.assertEqual(data["tool_uses_with_input"][0][1], {"patch": "diff --git"})
        finally:
            path.unlink()

    def test_invalid_arguments_fall_back_to_empty(self):
        path = _write_jsonl([
            _session_meta(),
            _function_call("grep", "not-json-nope", call_id="fc_c"),
        ])
        try:
            data = codex_adapter.load_session(path, {})
            name, inp = data["tool_uses_with_input"][0]
            self.assertEqual(name, "grep")
            self.assertEqual(inp, {})
        finally:
            path.unlink()


class LocalShellAndWebSearchTests(unittest.TestCase):
    def test_local_shell_call_joins_command(self):
        path = _write_jsonl([
            _session_meta(),
            _local_shell_call(["bash", "-lc", "ls -la"]),
        ])
        try:
            data = codex_adapter.load_session(path, {})
            self.assertEqual(data["tool_names"], ["local_shell_call"])
            name, inp = data["tool_uses_with_input"][0]
            self.assertEqual(name, "local_shell_call")
            self.assertEqual(inp["command"], "bash -lc ls -la")
        finally:
            path.unlink()

    def test_web_search_call_carries_query(self):
        path = _write_jsonl([
            _session_meta(),
            _web_search_call("python asyncio cancellation"),
        ])
        try:
            data = codex_adapter.load_session(path, {})
            _, inp = data["tool_uses_with_input"][0]
            self.assertEqual(inp, {"query": "python asyncio cancellation"})
        finally:
            path.unlink()


class ToolResultTests(unittest.TestCase):
    def test_function_call_output_becomes_tool_result(self):
        path = _write_jsonl([
            _session_meta(),
            _function_call("read_file", {"path": "x"}, call_id="fc_x"),
            _function_call_output("fc_x", "hello world"),
        ])
        try:
            data = codex_adapter.load_session(path, {})
            self.assertEqual(len(data["user_msgs"]), 1)
            blocks = data["user_msgs"][0]["message"]["content"]
            self.assertEqual(blocks[0]["type"], "tool_result")
            self.assertEqual(blocks[0]["tool_use_id"], "fc_x")
            self.assertEqual(blocks[0]["content"], "hello world")
            self.assertFalse(blocks[0]["is_error"])
        finally:
            path.unlink()

    def test_error_output_flags_is_error(self):
        path = _write_jsonl([
            _session_meta(),
            _function_call("read_file", {"path": "missing"}, call_id="fc_e"),
            _function_call_output("fc_e", "Error: file not found"),
        ])
        try:
            data = codex_adapter.load_session(path, {})
            blocks = data["user_msgs"][0]["message"]["content"]
            self.assertTrue(blocks[0]["is_error"])
        finally:
            path.unlink()


class TokenCountAttachmentTests(unittest.TestCase):
    def test_token_count_attaches_to_last_assistant_msg(self):
        path = _write_jsonl([
            _session_meta(),
            _reasoning("thinking"),
            _function_call("read_file", {"path": "a"}, call_id="fc_1"),
            _token_count(input_tokens=123, cached=77, output=456),
        ])
        try:
            data = codex_adapter.load_session(path, {})
            self.assertEqual(len(data["assistant_msgs"]), 1)
            usage = data["assistant_msgs"][-1]["message"]["usage"]
            self.assertEqual(usage["input_tokens"], 123)
            self.assertEqual(usage["cache_read_input_tokens"], 77)
            self.assertEqual(usage["output_tokens"], 456)
            # Claude-shape requires the key even if 0.
            self.assertEqual(usage["cache_creation_input_tokens"], 0)
        finally:
            path.unlink()

    def test_turn_bundles_reasoning_and_function_calls(self):
        """Consecutive reasoning + function_call belong to the SAME assistant msg."""
        path = _write_jsonl([
            _session_meta(),
            _user_msg("hi"),
            _reasoning("I will list first"),
            _function_call("read_file", {"path": "a"}, call_id="fc_1"),
            _function_call("read_file", {"path": "b"}, call_id="fc_2"),
            _token_count(),
            _function_call_output("fc_1", "A"),
            _function_call_output("fc_2", "B"),
        ])
        try:
            data = codex_adapter.load_session(path, {})
            # One assistant msg bundling reasoning + two function_calls.
            self.assertEqual(len(data["assistant_msgs"]), 1)
            content = data["assistant_msgs"][0]["message"]["content"]
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            self.assertEqual(len(tool_uses), 2)
            # Tool names still counted correctly.
            self.assertEqual(len(data["tool_names"]), 2)
            # Two tool_results on the user side.
            self.assertEqual(len(data["user_msgs"]), 3)  # 1 user_msg + 2 tool_results
        finally:
            path.unlink()


class IterEventsTests(unittest.TestCase):
    def test_iter_events_yields_canonical_events(self):
        path = _write_jsonl([
            _session_meta(),
            _user_msg("hi"),
            _reasoning("let me read"),
            _function_call("read_file", {"path": "x"}, call_id="fc_1"),
            _token_count(),
            _function_call_output("fc_1", "data"),
        ])
        try:
            events = list(codex_adapter.iter_events(path))
            kinds = [e.kind for e in events]
            self.assertIn("system", kinds)
            self.assertIn("user_message", kinds)
            self.assertIn("tool_call", kinds)
            self.assertIn("tool_result", kinds)
            for ev in events:
                self.assertIsInstance(ev, CanonicalEvent)
                self.assertEqual(ev.provider, "codex")
            tool_calls = [e for e in events if e.kind == "tool_call"]
            self.assertGreater(len(tool_calls), 0)
            for tc in tool_calls:
                self.assertIsNotNone(tc.tool_name)
                self.assertIsNotNone(tc.tool_family)
            # read_file maps to family "read".
            self.assertEqual(tool_calls[0].tool_family, "read")
        finally:
            path.unlink()


class EdgeCaseTests(unittest.TestCase):
    def test_malformed_lines_are_skipped(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        tmp.write(json.dumps(_session_meta()) + "\n")
        tmp.write("not-json-at-all\n")
        tmp.write(json.dumps(_function_call("read_file", {"p": 1}, call_id="fc_1")) + "\n")
        tmp.close()
        path = Path(tmp.name)
        try:
            data = codex_adapter.load_session(path, {})
            self.assertEqual(data["jsonl_lines"], 2)
            self.assertEqual(data["tool_names"], ["read_file"])
        finally:
            path.unlink()

    def test_empty_file_returns_empty_shape(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        tmp.close()
        path = Path(tmp.name)
        try:
            data = codex_adapter.load_session(path, {})
            self.assertEqual(data["jsonl_lines"], 0)
            self.assertEqual(data["records"], [])
            self.assertEqual(data["assistant_msgs"], [])
            self.assertEqual(data["user_msgs"], [])
            self.assertIsNone(data["session_id"])
            self.assertIsNone(data["project_dir"])
        finally:
            path.unlink()

    def test_turn_context_is_ignored(self):
        path = _write_jsonl([
            _session_meta(),
            {"timestamp": "2026-04-19T00:00:09.000Z", "type": "turn_context", "payload": {"foo": "bar"}},
            _function_call("read_file", {"p": 1}, call_id="fc_1"),
        ])
        try:
            data = codex_adapter.load_session(path, {})
            self.assertEqual(len(data["assistant_msgs"]), 1)
            self.assertEqual(data["tool_names"], ["read_file"])
        finally:
            path.unlink()


class ScorerCompatibilityTests(unittest.TestCase):
    """Smoke-test that score_jsonl consumes the Codex adapter output without raising.

    We pass provider="codex" explicitly so the dispatcher routes through the
    Codex adapter. This validates the Claude-shape contract end-to-end.
    """

    def test_score_jsonl_runs_on_codex_fixture(self):
        import agent_dashcam_score

        path = _write_jsonl([
            _session_meta(),
            _user_msg("please run the tests"),
            _reasoning("I will run pytest"),
            _local_shell_call(["bash", "-lc", "pytest -q"]),
            _token_count(input_tokens=2000, cached=1500, output=300),
            _function_call_output("lsc_1", "1 passed"),
            _assistant_message("all tests passed"),
        ])
        try:
            # Minimal config — scorer falls back to defaults for missing keys.
            result = agent_dashcam_score.score_jsonl(path, {}, provider="codex")
            self.assertIn("axes", result)
            self.assertIn("weighted_avg", result)
            self.assertEqual(
                result["meta"]["sessionId"],
                "11111111-1111-1111-1111-111111111111",
            )
            self.assertEqual(result["meta"]["project_dir"], "/Users/fixture/project")
            # Every axis must be in [0, 1].
            for name, val in result["axes"].items():
                self.assertGreaterEqual(val, 0.0, f"{name} < 0")
                self.assertLessEqual(val, 1.0, f"{name} > 1")
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
