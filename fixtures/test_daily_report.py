"""Agent Dashcam daily_report.py — worst-sessions helpers + render hooks 단위 테스트.

Covers the new feature that surfaces problematic session IDs in daily/weekly
Slack reports so the user can run `claude --resume <id>` (or the provider-
specific equivalent) to inspect the session directly.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


THIS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import daily_report


def _mk_score(
    weighted_avg: float | None,
    session_id: str | None = "sess-a",
    provider: str | None = None,
    axes: dict | None = None,
    suppressed: list[str] | None = None,
) -> dict:
    meta: dict = {}
    if session_id is not None:
        meta["sessionId"] = session_id
    if provider is not None:
        meta["provider"] = provider
    if suppressed is not None:
        meta["suppressed_axes"] = suppressed
    return {
        "weighted_avg": weighted_avg,
        "axes": axes or {
            "context_efficiency": 0.8,
            "cost_efficiency": 0.5,
            "role_focus": 0.4,
            "read_edit_ratio": 0.7,
        },
        "meta": meta,
    }


class ResumeCommandTests(unittest.TestCase):
    def test_claude_provider(self):
        self.assertEqual(
            daily_report._resume_command("claude", "abc-123"),
            "claude --resume abc-123",
        )

    def test_codex_provider(self):
        self.assertEqual(
            daily_report._resume_command("codex", "xyz-789"),
            "codex --resume xyz-789",
        )

    def test_gemini_provider_points_at_session_file(self):
        cmd = daily_report._resume_command("gemini", "gem-42")
        self.assertIn("gemini", cmd.lower())
        self.assertIn("gem-42", cmd)

    def test_none_provider_defaults_to_claude(self):
        self.assertEqual(
            daily_report._resume_command(None, "fallback-id"),
            "claude --resume fallback-id",
        )

    def test_unknown_provider_defaults_to_claude(self):
        self.assertEqual(
            daily_report._resume_command("mystery", "id-1"),
            "claude --resume id-1",
        )

    def test_provider_key_is_case_insensitive(self):
        self.assertEqual(
            daily_report._resume_command("CLAUDE", "u-1"),
            "claude --resume u-1",
        )


class LowestAxisForScoreTests(unittest.TestCase):
    def test_returns_lowest_axis(self):
        score = _mk_score(0.5, axes={"a": 0.9, "b": 0.2, "c": 0.5})
        self.assertEqual(daily_report._lowest_axis_for_score(score), "b")

    def test_excludes_suppressed_axes(self):
        score = _mk_score(
            0.5,
            axes={"a": 0.9, "b": 0.1, "c": 0.5},
            suppressed=["b"],
        )
        self.assertEqual(daily_report._lowest_axis_for_score(score), "c")

    def test_returns_none_when_no_axes(self):
        self.assertIsNone(daily_report._lowest_axis_for_score({"axes": {}}))

    def test_ignores_non_numeric(self):
        score = _mk_score(
            0.5,
            axes={"a": "bad", "b": None, "c": 0.3},
        )
        self.assertEqual(daily_report._lowest_axis_for_score(score), "c")


class WorstSessionsTests(unittest.TestCase):
    def test_filters_below_threshold_only(self):
        scores = [
            _mk_score(0.3, session_id="s-bad"),
            _mk_score(0.5, session_id="s-edge"),  # at threshold → excluded
            _mk_score(0.7, session_id="s-good"),
        ]
        result = daily_report._worst_sessions(scores, 0.5, 3)
        self.assertEqual([s["meta"]["sessionId"] for s in result], ["s-bad"])

    def test_sorts_worst_first(self):
        scores = [
            _mk_score(0.4, session_id="mid"),
            _mk_score(0.1, session_id="worst"),
            _mk_score(0.3, session_id="second"),
        ]
        result = daily_report._worst_sessions(scores, 0.5, 3)
        self.assertEqual(
            [s["meta"]["sessionId"] for s in result],
            ["worst", "second", "mid"],
        )

    def test_caps_at_max_count(self):
        scores = [_mk_score(0.1 * i, session_id=f"s-{i}") for i in range(5)]
        result = daily_report._worst_sessions(scores, 1.0, 2)
        self.assertEqual(len(result), 2)

    def test_skips_sessions_without_session_id(self):
        scores = [
            _mk_score(0.2, session_id=None),
            _mk_score(0.3, session_id="visible"),
        ]
        result = daily_report._worst_sessions(scores, 0.5, 3)
        self.assertEqual([s["meta"]["sessionId"] for s in result], ["visible"])

    def test_ignores_non_numeric_weighted_avg(self):
        scores = [
            _mk_score(None, session_id="nan"),
            _mk_score(0.2, session_id="real"),
        ]
        result = daily_report._worst_sessions(scores, 0.5, 3)
        self.assertEqual([s["meta"]["sessionId"] for s in result], ["real"])

    def test_zero_max_count_returns_empty(self):
        scores = [_mk_score(0.1, session_id="x")]
        self.assertEqual(daily_report._worst_sessions(scores, 0.5, 0), [])


class WorstSessionsRenderTests(unittest.TestCase):
    def test_render_md_empty_returns_empty_list(self):
        self.assertEqual(daily_report.render_worst_sessions_md([]), [])

    def test_render_md_contains_session_id_and_resume_command(self):
        worst = [_mk_score(0.25, session_id="abcd-1234", provider="codex")]
        lines = daily_report.render_worst_sessions_md(worst)
        text = "\n".join(lines)
        self.assertIn("Sessions that need a look", text)
        self.assertIn("abcd-1234", text)
        self.assertIn("codex --resume abcd-1234", text)

    def test_render_block_empty_returns_none(self):
        self.assertIsNone(daily_report.render_worst_sessions_block([]))

    def test_render_block_shape_and_payload(self):
        worst = [_mk_score(0.2, session_id="sid-xyz", provider="claude")]
        block = daily_report.render_worst_sessions_block(worst)
        self.assertEqual(block["type"], "section")
        self.assertEqual(block["text"]["type"], "mrkdwn")
        text = block["text"]["text"]
        self.assertIn("sid-xyz", text)
        self.assertIn("claude --resume sid-xyz", text)

    def test_render_block_unknown_provider_falls_back_to_claude(self):
        worst = [_mk_score(0.2, session_id="u-1", provider=None)]
        block = daily_report.render_worst_sessions_block(worst)
        self.assertIn("claude --resume u-1", block["text"]["text"])


if __name__ == "__main__":
    unittest.main()
