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


def _mk_score_with_attribution(attribution: dict | None) -> dict:
    return {
        "weighted_avg": 0.5,
        "axes": {"a": 0.5},
        "meta": {"agent_attribution": attribution} if attribution is not None else {},
    }


class AggregateAgentAttributionTests(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(daily_report._aggregate_agent_attribution([]), [])

    def test_sums_across_sessions(self):
        scores = [
            _mk_score_with_attribution({
                "architect": {"total_tokens": 1000, "tool_uses": 5, "duration_ms": 2000, "call_count": 1},
            }),
            _mk_score_with_attribution({
                "architect": {"total_tokens": 500, "tool_uses": 2, "duration_ms": 1000, "call_count": 1},
                "Explore":   {"total_tokens": 800, "tool_uses": 3, "duration_ms": 1500, "call_count": 1},
            }),
        ]
        agg = daily_report._aggregate_agent_attribution(scores)
        by_name = {e["subagent_type"]: e for e in agg}
        self.assertEqual(by_name["architect"]["total_tokens"], 1500)
        self.assertEqual(by_name["architect"]["call_count"], 2)
        self.assertEqual(by_name["architect"]["tool_uses"], 7)
        self.assertEqual(by_name["architect"]["duration_ms"], 3000)
        self.assertEqual(by_name["Explore"]["total_tokens"], 800)

    def test_sorted_by_tokens_descending(self):
        scores = [
            _mk_score_with_attribution({
                "light":  {"total_tokens": 100, "tool_uses": 1, "duration_ms": 100, "call_count": 1},
                "heavy":  {"total_tokens": 9000, "tool_uses": 10, "duration_ms": 5000, "call_count": 2},
                "medium": {"total_tokens": 2000, "tool_uses": 4, "duration_ms": 1200, "call_count": 1},
            }),
        ]
        agg = daily_report._aggregate_agent_attribution(scores)
        self.assertEqual([e["subagent_type"] for e in agg], ["heavy", "medium", "light"])

    def test_skips_sessions_without_attribution(self):
        scores = [
            {"weighted_avg": 0.5, "meta": {}},
            {"weighted_avg": 0.5, "meta": {"agent_attribution": None}},
            _mk_score_with_attribution({
                "only": {"total_tokens": 42, "tool_uses": 1, "duration_ms": 1, "call_count": 1},
            }),
        ]
        agg = daily_report._aggregate_agent_attribution(scores)
        self.assertEqual(len(agg), 1)
        self.assertEqual(agg[0]["subagent_type"], "only")
        self.assertEqual(agg[0]["total_tokens"], 42)

    def test_ignores_malformed_values(self):
        scores = [
            _mk_score_with_attribution({
                "bad": {"total_tokens": "not-a-number", "tool_uses": None, "duration_ms": [], "call_count": 1},
            }),
        ]
        agg = daily_report._aggregate_agent_attribution(scores)
        self.assertEqual(len(agg), 1)
        self.assertEqual(agg[0]["total_tokens"], 0)
        self.assertEqual(agg[0]["call_count"], 1)


class FormatHelperTests(unittest.TestCase):
    def test_fmt_tokens_scales(self):
        self.assertEqual(daily_report._fmt_tokens(500), "500")
        self.assertEqual(daily_report._fmt_tokens(1500), "1.5k")
        self.assertEqual(daily_report._fmt_tokens(2_500_000), "2.5M")

    def test_fmt_duration_ms_scales(self):
        self.assertEqual(daily_report._fmt_duration_ms(500), "0.5s")
        self.assertEqual(daily_report._fmt_duration_ms(3000), "3.0s")
        self.assertEqual(daily_report._fmt_duration_ms(120_000), "2.0m")


class SubagentBreakdownRenderTests(unittest.TestCase):
    def _sample_agg(self) -> list[dict]:
        return [
            {"subagent_type": "architect", "total_tokens": 12000, "tool_uses": 8, "duration_ms": 45000, "call_count": 1},
            {"subagent_type": "Explore",   "total_tokens": 8000,  "tool_uses": 5, "duration_ms": 12000, "call_count": 2},
        ]

    def test_md_empty_returns_empty(self):
        self.assertEqual(daily_report.render_subagent_breakdown_md([]), [])

    def test_md_top_n_zero_returns_empty(self):
        self.assertEqual(daily_report.render_subagent_breakdown_md(self._sample_agg(), 0), [])

    def test_md_contains_heading_and_rows(self):
        lines = daily_report.render_subagent_breakdown_md(self._sample_agg())
        text = "\n".join(lines)
        self.assertIn("Subagent cost breakdown", text)
        self.assertIn("architect", text)
        self.assertIn("12.0k", text)
        self.assertIn("45.0s", text)
        self.assertIn("Explore", text)

    def test_md_respects_top_n_cap(self):
        lines = daily_report.render_subagent_breakdown_md(self._sample_agg(), 1)
        text = "\n".join(lines)
        self.assertIn("architect", text)
        self.assertNotIn("Explore", text)

    def test_block_empty_returns_none(self):
        self.assertIsNone(daily_report.render_subagent_breakdown_block([]))

    def test_block_shape_and_payload(self):
        block = daily_report.render_subagent_breakdown_block(self._sample_agg())
        self.assertEqual(block["type"], "section")
        self.assertEqual(block["text"]["type"], "mrkdwn")
        text = block["text"]["text"]
        self.assertIn("Subagent cost breakdown", text)
        self.assertIn("architect", text)
        self.assertIn("12.0k tokens", text)
        self.assertIn("1 call", text)
        self.assertIn("2 calls", text)

    def test_block_singular_call_label(self):
        agg = [{"subagent_type": "solo", "total_tokens": 100, "tool_uses": 1, "duration_ms": 500, "call_count": 1}]
        block = daily_report.render_subagent_breakdown_block(agg)
        self.assertIn("1 call ", block["text"]["text"])
        self.assertNotIn("1 calls", block["text"]["text"])


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
