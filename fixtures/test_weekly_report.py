"""Agent Dashcam weekly_report.py 단위 테스트."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


THIS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _write_score(dir_: Path, name: str, mtime: float, weighted_avg: float, axes: dict | None = None) -> Path:
    p = dir_ / name
    payload = {
        "weighted_avg": weighted_avg,
        "axes": axes or {
            "context_efficiency": 0.7,
            "cost_efficiency": 0.5,
            "cost_per_useful_output": 0.4,
            "role_focus": 0.5,
            "read_edit_ratio": 0.6,
            "reasoning_loop": 0.7,
            "sentiment": 0.7,
            "constraint_adherence": 0.9,
            "hook_health": 0.9,
            "operational_bottleneck": 0.5,
        },
        "meta": {
            "sessionId": name.replace(".json", ""),
            "scored_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    }
    p.write_text(json.dumps(payload))
    os.utime(p, (mtime, mtime))
    return p


class LoadScoresInWindowTests(unittest.TestCase):
    def test_filters_by_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            scores_dir = Path(td) / "scores"
            scores_dir.mkdir()
            now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
            # within 7d
            _write_score(scores_dir, "a.json", (now - timedelta(days=2)).timestamp(), 0.6)
            _write_score(scores_dir, "b.json", (now - timedelta(days=6)).timestamp(), 0.5)
            # out of window (older)
            _write_score(scores_dir, "c.json", (now - timedelta(days=10)).timestamp(), 0.4)
            # in the future (after end) — should be excluded
            _write_score(scores_dir, "d.json", (now + timedelta(hours=1)).timestamp(), 0.9)

            with mock.patch("daily_report.SCORES_DIR", scores_dir):
                import importlib
                import weekly_report
                importlib.reload(weekly_report)
                result = weekly_report.load_scores_in_window(now, days=7)

            names = {Path(s["_path"]).name for s in result}
            self.assertEqual(names, {"a.json", "b.json"})
            # sorted by _mtime ascending
            self.assertLess(result[0]["_mtime"], result[1]["_mtime"])

    def test_missing_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            ghost = Path(td) / "does-not-exist"
            with mock.patch("daily_report.SCORES_DIR", ghost):
                import importlib
                import weekly_report
                importlib.reload(weekly_report)
                self.assertEqual(weekly_report.load_scores_in_window(datetime.now(timezone.utc), 7), [])


class SessionsByDayTests(unittest.TestCase):
    def test_bucketing(self):
        import weekly_report
        now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
        scores = [
            {"_mtime": (now - timedelta(days=0, hours=2)).timestamp()},
            {"_mtime": (now - timedelta(days=0, hours=5)).timestamp()},
            {"_mtime": (now - timedelta(days=2)).timestamp()},
            {"_mtime": (now - timedelta(days=6)).timestamp()},
        ]
        by_day = weekly_report.sessions_by_day(scores, now, days=7)
        self.assertEqual(len(by_day), 7)
        # oldest first
        self.assertEqual(by_day[0][0], (now - timedelta(days=6)).strftime("%Y-%m-%d"))
        self.assertEqual(by_day[-1][0], now.strftime("%Y-%m-%d"))
        # counts
        counts = {d: c for d, c in by_day}
        self.assertEqual(counts[now.strftime("%Y-%m-%d")], 2)
        self.assertEqual(counts[(now - timedelta(days=2)).strftime("%Y-%m-%d")], 1)
        self.assertEqual(counts[(now - timedelta(days=6)).strftime("%Y-%m-%d")], 1)


class SparklineTests(unittest.TestCase):
    def test_empty(self):
        import weekly_report
        self.assertEqual(weekly_report.sparkline([]), "")

    def test_all_zero(self):
        import weekly_report
        # all zero → first char × length
        s = weekly_report.sparkline([0, 0, 0, 0])
        self.assertEqual(len(s), 4)

    def test_proportional(self):
        import weekly_report
        s = weekly_report.sparkline([0, 1, 2, 4, 8])
        self.assertEqual(len(s), 5)
        # max value should use last char
        self.assertEqual(s[-1], "█")


class ComboFrequencyTests(unittest.TestCase):
    def test_counts_opus_overuse(self):
        import weekly_report
        bad = {
            "cost_efficiency": 0.2,
            "role_focus": 0.2,
            "context_efficiency": 0.7,
            "cost_per_useful_output": 0.5,
            "read_edit_ratio": 0.5,
            "reasoning_loop": 0.7,
            "sentiment": 0.7,
            "constraint_adherence": 0.9,
            "hook_health": 0.9,
            "operational_bottleneck": 0.7,
        }
        good = {**bad, "cost_efficiency": 0.7, "role_focus": 0.7}
        scores = [
            {"weighted_avg": 0.3, "axes": bad},
            {"weighted_avg": 0.3, "axes": bad},
            {"weighted_avg": 0.7, "axes": good},
        ]
        freq = weekly_report.combo_frequency(scores)
        by_id = {c["id"]: c for c in freq}
        self.assertEqual(by_id["opus_overuse"]["count"], 2)
        self.assertAlmostEqual(by_id["opus_overuse"]["rate"], 2 / 3, places=2)

    def test_counts_golden(self):
        import weekly_report
        golden_axes = {a: 0.8 for a in [
            "context_efficiency", "cost_efficiency", "cost_per_useful_output",
            "role_focus", "read_edit_ratio", "reasoning_loop", "sentiment",
            "constraint_adherence", "hook_health", "operational_bottleneck",
        ]}
        scores = [{"weighted_avg": 0.8, "axes": golden_axes}]
        freq = weekly_report.combo_frequency(scores)
        by_id = {c["id"]: c for c in freq}
        self.assertEqual(by_id["golden"]["count"], 1)


class GoldenRateTests(unittest.TestCase):
    def test_rate(self):
        import weekly_report
        scores = [
            {"weighted_avg": 0.8},
            {"weighted_avg": 0.76},
            {"weighted_avg": 0.5},
            {"weighted_avg": 0.3},
        ]
        hits, rate = weekly_report.golden_session_stats(scores)
        self.assertEqual(hits, 2)
        self.assertAlmostEqual(rate, 0.5, places=2)

    def test_empty(self):
        import weekly_report
        hits, rate = weekly_report.golden_session_stats([])
        self.assertEqual(hits, 0)
        self.assertEqual(rate, 0.0)


class WowDeltaTests(unittest.TestCase):
    def test_normal(self):
        import weekly_report
        d = weekly_report.wow_delta(0.6, 0.5)
        self.assertAlmostEqual(d["delta"], 0.1, places=4)

    def test_missing_side(self):
        import weekly_report
        self.assertIsNone(weekly_report.wow_delta(None, 0.5)["delta"])
        self.assertIsNone(weekly_report.wow_delta(0.6, None)["delta"])


class TopBottomSessionsTests(unittest.TestCase):
    def test_picks_best_worst(self):
        import weekly_report
        scores = [
            {"weighted_avg": 0.4, "meta": {"sessionId": "lo"}},
            {"weighted_avg": 0.9, "meta": {"sessionId": "hi"}},
            {"weighted_avg": 0.6, "meta": {"sessionId": "mid"}},
        ]
        best, worst = weekly_report.top_bottom_sessions(scores, k=1)
        self.assertEqual(best[0]["weighted_avg"], 0.9)
        self.assertEqual(worst[0]["weighted_avg"], 0.4)


class RenderE2ETests(unittest.TestCase):
    def _scores(self):
        axes = {
            "context_efficiency": 0.7,
            "cost_efficiency": 0.3,
            "cost_per_useful_output": 0.5,
            "role_focus": 0.3,
            "read_edit_ratio": 0.6,
            "reasoning_loop": 0.7,
            "sentiment": 0.7,
            "constraint_adherence": 0.9,
            "hook_health": 0.9,
            "operational_bottleneck": 0.5,
        }
        now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
        return now, [
            {
                "weighted_avg": 0.6 + 0.01 * i,
                "axes": axes,
                "_mtime": (now - timedelta(days=i)).timestamp(),
                "meta": {"sessionId": f"s{i}", "scored_at": now.isoformat().replace("+00:00", "Z")},
            }
            for i in range(5)
        ]

    def test_markdown_renders(self):
        import weekly_report
        from daily_report import compute_axis_stats
        now, scores = self._scores()
        stats = compute_axis_stats(scores)
        prev = scores[:2]
        md = weekly_report.render_markdown(now, 7, scores, prev, stats)
        self.assertIn("Agent Dashcam weekly", md)
        self.assertIn("week-over-week", md)
        self.assertIn("golden sessions", md)
        self.assertIn("Combo pattern frequency", md)
        self.assertIn("activity by day", md)

    def test_slack_payload_renders(self):
        import weekly_report
        from daily_report import compute_axis_stats
        now, scores = self._scores()
        stats = compute_axis_stats(scores)
        prev = scores[:2]
        payload = weekly_report.render_slack_payload(now, 7, scores, prev, stats, "C_TEST")
        self.assertEqual(payload["channel"], "C_TEST")
        self.assertIn("weekly", payload["text"])
        types = [b["type"] for b in payload["blocks"]]
        self.assertIn("header", types)
        self.assertIn("context", types)
        # must contain weekly-specific signals
        full = json.dumps(payload)
        self.assertIn("week-over-week", full)
        self.assertIn("activity", full)
        self.assertIn("golden sessions", full)


if __name__ == "__main__":
    unittest.main()
