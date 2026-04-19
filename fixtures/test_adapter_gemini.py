#!/usr/bin/env python3
"""adapter_gemini unit tests.

Verifies:
  - Gemini snake_case tool names normalize via canonicalize_tool_name
  - load_session returns the Claude-shaped dict the scorer expects
  - Session id extracted from the `session-<uuid>.json` filename
  - Gemini model parts (text + functionCall + functionResponse) project correctly
  - usageMetadata maps to Claude usage shape
  - functionResponse with `error` becomes is_error=true
  - $rewindTo / $set control markers are tolerated
  - iter_events yields tool_calls with tool_family populated
  - Malformed lines are skipped; empty files return empty-shaped dicts
  - Scorer integration: agent_dashcam_score.score_jsonl consumes a synthetic Gemini fixture
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

from adapters import gemini as gemini_adapter  # noqa: E402
from canonical import CanonicalEvent, canonicalize_tool_name  # noqa: E402


def _write_jsonl(tmpdir: Path, name: str, records: list[dict]) -> Path:
    """Write a list of records as JSONL. Name mimics Gemini's `session-<uuid>.json`."""
    path = tmpdir / name
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def _default_records() -> list[dict]:
    return [
        {"role": "user", "parts": [{"text": "please read the readme"}],
         "metadata": {"timestamp": "2026-04-19T00:00:00Z", "cwd": "/tmp/proj"}},
        {"role": "model", "parts": [
            {"text": "sure, reading now"},
            {"functionCall": {"name": "read_file", "args": {"path": "README.md"}}},
        ], "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 20,
            "totalTokenCount": 120,
            "cachedContentTokenCount": 50,
        }, "metadata": {"timestamp": "2026-04-19T00:00:01Z", "model": "gemini-2.5-pro"}},
        {"role": "model", "parts": [
            {"functionResponse": {"name": "read_file", "response": {"output": "# README\n", "error": None}}},
        ], "metadata": {"timestamp": "2026-04-19T00:00:02Z"}},
    ]


class ToolFamilyMapTests(unittest.TestCase):
    def test_gemini_tool_names_map(self):
        self.assertEqual(canonicalize_tool_name("read_file"), "read")
        self.assertEqual(canonicalize_tool_name("write_file"), "write")
        self.assertEqual(canonicalize_tool_name("replace"), "edit")
        self.assertEqual(canonicalize_tool_name("run_shell_command"), "bash")
        self.assertEqual(canonicalize_tool_name("grep_search"), "grep")
        self.assertEqual(canonicalize_tool_name("glob"), "glob")
        self.assertEqual(canonicalize_tool_name("web_fetch"), "web_fetch")
        self.assertEqual(canonicalize_tool_name("web_search"), "web_search")
        self.assertEqual(canonicalize_tool_name("save_memory"), "todo")


class LoadSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_expected_keys(self):
        path = _write_jsonl(self.tmpdir, "session-abc123.json", _default_records())
        data = gemini_adapter.load_session(path, {})
        expected = {
            "provider", "records", "partial",
            "assistant_msgs", "user_msgs", "progress_msgs", "system_msgs",
            "tool_names", "tool_uses_with_input",
            "assistant_text_lc", "user_text_lc",
            "session_id", "project_dir",
            "jsonl_lines", "jsonl_bytes",
        }
        self.assertTrue(expected.issubset(set(data.keys())))

    def test_provider_is_gemini(self):
        path = _write_jsonl(self.tmpdir, "session-abc123.json", _default_records())
        data = gemini_adapter.load_session(path, {})
        self.assertEqual(data["provider"], "gemini")

    def test_session_id_from_filename(self):
        path = _write_jsonl(self.tmpdir, "session-abc123.json", _default_records())
        data = gemini_adapter.load_session(path, {})
        self.assertEqual(data["session_id"], "abc123")

    def test_session_id_from_uuid_filename(self):
        path = _write_jsonl(self.tmpdir, "session-550e8400-e29b-41d4-a716-446655440000.json", [])
        data = gemini_adapter.load_session(path, {})
        self.assertEqual(data["session_id"], "550e8400-e29b-41d4-a716-446655440000")

    def test_mixed_text_and_function_call_one_assistant_msg(self):
        path = _write_jsonl(self.tmpdir, "session-abc123.json", _default_records())
        data = gemini_adapter.load_session(path, {})
        # The model event (text + functionCall) should yield exactly ONE assistant msg
        # with TWO content blocks. The trailing functionResponse event yields a separate
        # model msg (empty content) + a user msg carrying the tool_result.
        first = data["assistant_msgs"][0]
        content = first["message"]["content"]
        types = [b.get("type") for b in content]
        self.assertEqual(types, ["text", "tool_use"])
        self.assertEqual(content[1]["name"], "read_file")
        self.assertEqual(content[1]["input"], {"path": "README.md"})

    def test_usage_metadata_maps_to_claude_usage(self):
        path = _write_jsonl(self.tmpdir, "session-abc123.json", _default_records())
        data = gemini_adapter.load_session(path, {})
        usage = data["assistant_msgs"][0]["message"]["usage"]
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertEqual(usage["cache_read_input_tokens"], 50)
        self.assertEqual(usage["cache_creation_input_tokens"], 0)

    def test_function_response_becomes_user_tool_result(self):
        path = _write_jsonl(self.tmpdir, "session-abc123.json", _default_records())
        data = gemini_adapter.load_session(path, {})
        # Find a user msg with tool_result content block
        found = False
        for msg in data["user_msgs"]:
            content = msg["message"]["content"]
            for block in content:
                if block.get("type") == "tool_result":
                    found = True
                    self.assertFalse(block["is_error"])
                    self.assertIn("README", block["content"])
        self.assertTrue(found)

    def test_function_response_with_error_sets_is_error(self):
        records = [{"role": "model", "parts": [
            {"functionResponse": {"name": "read_file", "response": {"output": "", "error": "ENOENT"}}},
        ]}]
        path = _write_jsonl(self.tmpdir, "session-err.json", records)
        data = gemini_adapter.load_session(path, {})
        tr_blocks = [b for m in data["user_msgs"]
                     for b in m["message"]["content"] if b.get("type") == "tool_result"]
        self.assertEqual(len(tr_blocks), 1)
        self.assertTrue(tr_blocks[0]["is_error"])

    def test_rewind_marker_does_not_crash(self):
        records = _default_records() + [
            {"$rewindTo": "event-xyz"},
            {"role": "user", "parts": [{"text": "continue"}]},
        ]
        path = _write_jsonl(self.tmpdir, "session-rewind.json", records)
        data = gemini_adapter.load_session(path, {})
        # rewind markers are kept as progress_msgs; msg counts remain sensible.
        self.assertEqual(len(data["progress_msgs"]), 1)
        self.assertGreaterEqual(len(data["user_msgs"]), 2)

    def test_set_marker_updates_project_dir(self):
        records = [
            {"$set": {"path": "metadata.cwd", "value": "/workspace/myproj"}},
            {"role": "user", "parts": [{"text": "hi"}]},
        ]
        path = _write_jsonl(self.tmpdir, "session-set.json", records)
        data = gemini_adapter.load_session(path, {})
        self.assertEqual(data["project_dir"], "/workspace/myproj")

    def test_malformed_lines_are_skipped(self):
        path = self.tmpdir / "session-bad.json"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "parts": [{"text": "hi"}]}) + "\n")
            f.write("not-json\n")
            f.write(json.dumps({"role": "model", "parts": [{"text": "hello"}],
                                "usageMetadata": {"promptTokenCount": 1,
                                                  "candidatesTokenCount": 1}}) + "\n")
        data = gemini_adapter.load_session(path, {})
        self.assertEqual(data["jsonl_lines"], 2)
        self.assertEqual(len(data["user_msgs"]), 1)
        self.assertEqual(len(data["assistant_msgs"]), 1)

    def test_empty_file_returns_empty_shape(self):
        path = self.tmpdir / "session-empty.json"
        path.write_text("")
        data = gemini_adapter.load_session(path, {})
        self.assertEqual(data["jsonl_lines"], 0)
        self.assertEqual(data["records"], [])
        self.assertEqual(data["assistant_msgs"], [])
        self.assertEqual(data["user_msgs"], [])
        self.assertIsNone(data["project_dir"])
        # session id still derives from filename.
        self.assertEqual(data["session_id"], "empty")

    def test_tool_names_extracted(self):
        path = _write_jsonl(self.tmpdir, "session-abc123.json", _default_records())
        data = gemini_adapter.load_session(path, {})
        self.assertIn("read_file", data["tool_names"])


class IterEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_yields_canonical_events(self):
        path = _write_jsonl(self.tmpdir, "session-abc123.json", _default_records())
        events = list(gemini_adapter.iter_events(path))
        self.assertGreater(len(events), 0)
        for ev in events:
            self.assertIsInstance(ev, CanonicalEvent)
            self.assertEqual(ev.provider, "gemini")

    def test_tool_call_populates_family(self):
        path = _write_jsonl(self.tmpdir, "session-abc123.json", _default_records())
        tool_calls = [e for e in gemini_adapter.iter_events(path) if e.kind == "tool_call"]
        self.assertEqual(len(tool_calls), 1)
        tc = tool_calls[0]
        self.assertEqual(tc.tool_name, "read_file")
        self.assertEqual(tc.tool_family, "read")
        self.assertEqual(tc.tool_input, {"path": "README.md"})

    def test_agent_message_carries_tokens(self):
        path = _write_jsonl(self.tmpdir, "session-abc123.json", _default_records())
        agents = [e for e in gemini_adapter.iter_events(path) if e.kind == "agent_message"]
        self.assertGreater(len(agents), 0)
        self.assertTrue(any(e.tokens_input == 100 and e.tokens_output == 20 for e in agents))
        self.assertTrue(any(e.tokens_cache_read == 50 for e in agents))


class ScorerIntegrationTests(unittest.TestCase):
    def test_score_jsonl_consumes_gemini_via_adapter(self):
        """Smoke test: route score_jsonl through the Gemini adapter via provider flag."""
        import agent_dashcam_score  # noqa: E402
        tmp = tempfile.TemporaryDirectory()
        try:
            path = _write_jsonl(Path(tmp.name), "session-smoke.json", _default_records())
            config = {
                "jsonl_tail_threshold_mb": 20,
                "jsonl_tail_lines": 5000,
                "model_rates": {"default": {"input": 1.0, "output": 1.0,
                                            "cache_read": 0.1, "cache_write": 1.0}},
                "cost_efficiency_thresholds": {"lo": 500.0, "hi": 50000.0},
                "read_edit_ratio_thresholds": {"degraded": 2.0, "good": 6.0},
                "cost_per_useful_thresholds": {"lo": 10.0, "hi": 0.10},
                "reasoning_loop_thresholds": {"degraded_per_1k": 20.0, "good_per_1k": 10.0},
                "scoring_weights": {},
            }
            result = agent_dashcam_score.score_jsonl(path, config, provider="gemini")
        finally:
            tmp.cleanup()
        self.assertIn("axes", result)
        self.assertIn("weighted_avg", result)
        self.assertEqual(result["meta"]["sessionId"], "smoke")


if __name__ == "__main__":
    unittest.main()
