#!/usr/bin/env python3
"""retention.py unit test — 120개 더미 파일로 100개 유지 확인."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

AGENT_DASHCAM_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = AGENT_DASHCAM_ROOT / "scripts" / "retention.py"

os.environ.setdefault("AGENT_DASHCAM_ROOT", str(AGENT_DASHCAM_ROOT))
sys.path.insert(0, str(AGENT_DASHCAM_ROOT / "scripts"))
import retention  # noqa: E402


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.scores = self.tmp_path / "scores"
        self.monthly = self.tmp_path / "monthly"
        self.logs = self.tmp_path / "logs"
        self.scores.mkdir()
        retention.SCORES_DIR = self.scores
        retention.MONTHLY_DIR = self.monthly
        retention.LOG_PATH = self.logs / "retention.log"

    def tearDown(self):
        self.tmp.cleanup()

    def _make_files(self, n: int, month: str = "2026-04"):
        for i in range(n):
            p = self.scores / f"proj__session-{i:04d}.json"
            data = {
                "axes": {
                    "context_efficiency": 0.5 + (i % 5) * 0.05,
                    "cost_efficiency": 0.3,
                    "role_focus": 0.8,
                    "constraint_adherence": 0.9,
                    "hook_health": 1.0,
                    "operational_bottleneck": 0.7,
                },
                "weighted_avg": 0.6,
                "meta": {"scored_at": f"{month}-01T00:00:{i:02d}.000Z", "sessionId": f"session-{i}"},
            }
            with open(p, "w") as f:
                json.dump(data, f)
            ts = time.time() - (n - i)
            import os
            os.utime(p, (ts, ts))

    def test_120_files_keep_100(self):
        self._make_files(120)
        result = retention.run_retention(limit=100, dry_run=False)
        self.assertEqual(result["deleted"], 20)
        remaining = list(self.scores.iterdir())
        self.assertEqual(len(remaining), 100)
        monthly_files = list(self.monthly.glob("monthly-summary-*.json"))
        self.assertEqual(len(monthly_files), 1)
        with open(monthly_files[0]) as f:
            summary = json.load(f)
        self.assertEqual(summary["session_count"], 20)
        self.assertIn("context_efficiency", summary["axes_stats"])

    def test_under_limit_noop(self):
        self._make_files(50)
        result = retention.run_retention(limit=100, dry_run=False)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["reason"], "under limit")

    def test_dry_run(self):
        self._make_files(120)
        result = retention.run_retention(limit=100, dry_run=True)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(len(list(self.scores.iterdir())), 120)


if __name__ == "__main__":
    unittest.main()
