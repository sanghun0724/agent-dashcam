#!/usr/bin/env python3
"""v3 axes unit tests — cost_per_useful_output / reasoning_loop / sentiment +
동적 캘리브레이션 + schema drift scan.

stdlib unittest만 사용.
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

import agent_dashcam_score  # noqa: E402
import retention  # noqa: E402
import envup  # noqa: E402


class CostPerUsefulOutputTests(unittest.TestCase):
    def test_zero_useful_returns_half(self):
        self.assertEqual(agent_dashcam_score.compute_cost_per_useful_output(5.0, 0), 0.5)

    def test_cheap_returns_one(self):
        # 1 output, $0.05 -> below hi=0.1
        self.assertEqual(agent_dashcam_score.compute_cost_per_useful_output(0.05, 1, lo=10.0, hi=0.10), 1.0)

    def test_expensive_returns_zero(self):
        self.assertEqual(agent_dashcam_score.compute_cost_per_useful_output(100.0, 1, lo=10.0, hi=0.10), 0.0)

    def test_middle_is_interpolated(self):
        # cost_per = 1.0, between hi=0.1 and lo=10
        score = agent_dashcam_score.compute_cost_per_useful_output(10.0, 10, lo=10.0, hi=0.10)
        self.assertTrue(0.0 < score < 1.0)

    def test_count_useful_outputs_basic(self):
        tool_uses = [
            ("Bash", {"command": "git commit -m 'ok'"}),
            ("Bash", {"command": "gh pr create"}),
            ("Bash", {"command": "pytest -v"}),
            ("Bash", {"command": "ls"}),
            ("Read", {"file_path": "x"}),
        ]
        r = agent_dashcam_score.count_useful_outputs(tool_uses)
        self.assertEqual(r["commits"], 1)
        self.assertEqual(r["prs"], 1)
        self.assertEqual(r["tests"], 1)
        self.assertEqual(r["total"], 3)

    def test_count_useful_outputs_empty(self):
        r = agent_dashcam_score.count_useful_outputs([])
        self.assertEqual(r["total"], 0)


class ReasoningLoopTests(unittest.TestCase):
    def test_no_patterns_returns_good(self):
        score, count, per_1k = agent_dashcam_score.compute_reasoning_loop("", 100)
        self.assertEqual(score, 0.5)
        self.assertEqual(count, 0)

    def test_clean_text_returns_one(self):
        text = "i completed the task and wrote tests"
        score, count, _ = agent_dashcam_score.compute_reasoning_loop(text, 1000)
        self.assertEqual(score, 1.0)
        self.assertEqual(count, 0)

    def test_heavy_retry_language_degrades(self):
        # 25 occurrences per 1000 tool calls should hit degraded (>=20/1k)
        text = ("let me try again " * 25)
        score, count, per_1k = agent_dashcam_score.compute_reasoning_loop(text, 1000)
        self.assertEqual(count, 25)
        self.assertEqual(per_1k, 25.0)
        self.assertEqual(score, 0.0)

    def test_moderate_retry_is_linear(self):
        # 15 per 1000 = middle between good(10) and degraded(20) -> 0.5
        text = ("let me try again " * 15)
        score, _, per_1k = agent_dashcam_score.compute_reasoning_loop(text, 1000)
        self.assertEqual(per_1k, 15.0)
        self.assertAlmostEqual(score, 0.5, places=3)


class SentimentTests(unittest.TestCase):
    def test_empty_returns_half(self):
        score, stats = agent_dashcam_score.compute_sentiment("")
        self.assertEqual(score, 0.5)
        self.assertEqual(stats["pos"], 0)
        self.assertEqual(stats["neg"], 0)

    def test_only_positive(self):
        score, stats = agent_dashcam_score.compute_sentiment("thanks great perfect awesome")
        self.assertEqual(score, 1.0)
        self.assertGreaterEqual(stats["pos"], 4)
        self.assertEqual(stats["neg"], 0)

    def test_only_negative(self):
        score, stats = agent_dashcam_score.compute_sentiment("wrong broken failed terrible 아니야 하지마")
        self.assertEqual(score, 0.0)
        self.assertGreaterEqual(stats["neg"], 4)

    def test_balanced_is_low(self):
        # 2 pos : 1 neg -> ratio 2, below degraded 3, score 0
        score, stats = agent_dashcam_score.compute_sentiment("thanks great wrong")
        self.assertEqual(stats["pos"], 2)
        self.assertEqual(stats["neg"], 1)
        self.assertEqual(score, 0.0)


class CalibrationTests(unittest.TestCase):
    def test_insufficient_samples_skips(self):
        cfg = {"calibration": {"enabled": True, "min_sessions": 30, "window": 90}}
        # scores dir may have <30 files in test env; function should return insufficient
        r = retention.calibrate_thresholds(cfg, dry_run=True)
        self.assertIn(r.get("status"), ("ok", "insufficient_samples"))

    def test_disabled_short_circuits(self):
        cfg = {"calibration": {"enabled": False}}
        r = retention.calibrate_thresholds(cfg, dry_run=True)
        self.assertEqual(r.get("status"), "disabled")

    def test_quantile_helper(self):
        self.assertAlmostEqual(retention._quantile([1, 2, 3, 4, 5], 0.20), 1.8, places=2)
        self.assertAlmostEqual(retention._quantile([1, 2, 3, 4, 5], 0.80), 4.2, places=2)
        self.assertEqual(retention._quantile([], 0.5), 0.0)
        self.assertEqual(retention._quantile([42], 0.5), 42)


class SchemaDriftScanTests(unittest.TestCase):
    def test_returns_shape(self):
        r = envup.scan_schema_drift(window=30)
        self.assertIn("scanned", r)
        self.assertIn("sessions_with_drift", r)
        self.assertIn("drift_fields", r)
        self.assertIn("recent", r)
        self.assertIsInstance(r["drift_fields"], dict)
        self.assertIsInstance(r["recent"], list)


class TenAxisIntegrationTests(unittest.TestCase):
    def test_fixture_yields_ten_axes(self):
        config = agent_dashcam_score.load_config()
        path = AGENT_DASHCAM_ROOT / "fixtures" / "sample_0100.jsonl"
        self.assertTrue(path.exists(), f"fixture missing: {path}")
        r = agent_dashcam_score.score_jsonl(path, config)
        self.assertEqual(len(r["axes"]), 10)
        for a in (
            "cost_per_useful_output",
            "reasoning_loop",
            "sentiment",
        ):
            self.assertIn(a, r["axes"])
            v = r["axes"][a]
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)
        meta = r["meta"]
        self.assertIn("useful_outputs", meta)
        self.assertIn("reasoning_loop_count", meta)
        self.assertIn("sentiment_stats", meta)

    def test_weights_sum_to_one(self):
        config = agent_dashcam_score.load_config()
        total = sum(config["scoring_weights"].values())
        self.assertAlmostEqual(total, 1.0, places=4)


class SessionTypeClassifierTests(unittest.TestCase):
    def test_empty_returns_mixed(self):
        t, c = agent_dashcam_score.classify_session_type([], [], {"commits": 0, "prs": 0, "tests": 0, "total": 0}, "")
        self.assertEqual(t, "mixed")
        self.assertEqual(c, 0.0)

    def test_commit_flags_feature(self):
        t, c = agent_dashcam_score.classify_session_type(
            ["Edit", "Bash"], [("Edit", {"file_path": "a.py"})],
            {"commits": 1, "prs": 0, "tests": 2, "total": 3}, "",
        )
        self.assertEqual(t, "feature")
        self.assertGreater(c, 0.6)

    def test_markdown_heavy_is_docs(self):
        uses = [("Edit", {"file_path": "readme.md"})] * 3 + [("Write", {"file_path": "guide.mdx"})]
        t, _ = agent_dashcam_score.classify_session_type(
            ["Edit", "Edit", "Edit", "Write"], uses,
            {"commits": 0, "prs": 0, "tests": 0, "total": 0}, "",
        )
        self.assertEqual(t, "docs")

    def test_read_heavy_is_explore(self):
        names = ["Read"] * 6 + ["Grep"] * 2
        t, _ = agent_dashcam_score.classify_session_type(
            names, [],
            {"commits": 0, "prs": 0, "tests": 0, "total": 0}, "",
        )
        self.assertEqual(t, "explore")

    def test_edit_heavy_no_test_is_refactor(self):
        names = ["Edit"] * 5 + ["Read"]
        uses = [("Edit", {"file_path": "x.py"})] * 5
        t, _ = agent_dashcam_score.classify_session_type(
            names, uses,
            {"commits": 0, "prs": 0, "tests": 0, "total": 0}, "",
        )
        self.assertEqual(t, "refactor")

    def test_bugfix_edit_with_tests(self):
        names = ["Edit", "Edit", "Bash"]
        uses = [("Edit", {"file_path": "x.py"}), ("Edit", {"file_path": "y.py"})]
        t, _ = agent_dashcam_score.classify_session_type(
            names, uses,
            {"commits": 0, "prs": 0, "tests": 2, "total": 2}, "",
        )
        self.assertEqual(t, "bugfix")

    def test_meta_config_edits(self):
        uses = [
            ("Edit", {"file_path": ".claude/settings.json"}),
            ("Edit", {"file_path": "config.toml"}),
            ("Edit", {"file_path": "pyproject.toml"}),
        ]
        names = ["Edit"] * 3 + ["Read"]
        t, _ = agent_dashcam_score.classify_session_type(
            names, uses,
            {"commits": 0, "prs": 0, "tests": 0, "total": 0}, "",
        )
        self.assertEqual(t, "meta")

    def test_suppression_table_coverage(self):
        # 모든 타입이 suppression table 에 존재해야 함 (mixed 포함)
        for typ in ("feature", "bugfix", "refactor", "explore", "debug", "docs", "meta", "mixed"):
            self.assertIn(typ, agent_dashcam_score.SESSION_TYPE_SUPPRESSIONS)


if __name__ == "__main__":
    unittest.main()
