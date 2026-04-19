#!/usr/bin/env python3
"""agent_dashcam_score.py unit test — 3개 fixture(10/100/1000줄) 기반."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

AGENT_DASHCAM_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = AGENT_DASHCAM_ROOT / "scripts" / "agent_dashcam_score.py"
FIXTURES = AGENT_DASHCAM_ROOT / "fixtures"

os.environ.setdefault("AGENT_DASHCAM_ROOT", str(AGENT_DASHCAM_ROOT))
sys.path.insert(0, str(AGENT_DASHCAM_ROOT / "scripts"))
import agent_dashcam_score  # noqa: E402


EXPECTED_AXES = {
    "context_efficiency",
    "cost_efficiency",
    "cost_per_useful_output",
    "role_focus",
    "read_edit_ratio",
    "reasoning_loop",
    "sentiment",
    "constraint_adherence",
    "hook_health",
    "operational_bottleneck",
}


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = agent_dashcam_score.load_config()

    def _score(self, name: str) -> dict:
        path = FIXTURES / name
        self.assertTrue(path.exists(), f"fixture missing: {path}")
        return agent_dashcam_score.score_jsonl(path, self.config)

    def _assert_valid_result(self, result: dict, expected_lines: int):
        self.assertIn("axes", result)
        self.assertEqual(set(result["axes"].keys()), EXPECTED_AXES)
        for axis, score in result["axes"].items():
            self.assertIsInstance(score, (int, float), f"{axis} should be numeric, got {type(score)}")
            self.assertGreaterEqual(score, 0.0, f"{axis} below 0")
            self.assertLessEqual(score, 1.0, f"{axis} above 1")
        self.assertIsInstance(result["weighted_avg"], (int, float))
        self.assertEqual(result["meta"]["jsonl_lines"], expected_lines)
        self.assertFalse(result["meta"]["partial_score"], "small fixture should not trigger tail")
        self.assertEqual(result["meta"]["schema_drift"], [], "fixture schema should match expected")

    def test_10_lines(self):
        r = self._score("sample_0010.jsonl")
        self._assert_valid_result(r, 10)

    def test_100_lines(self):
        r = self._score("sample_0100.jsonl")
        self._assert_valid_result(r, 100)

    def test_1000_lines(self):
        r = self._score("sample_1000.jsonl")
        self._assert_valid_result(r, 1000)

    def test_cli_smoke(self):
        p = FIXTURES / "sample_0100.jsonl"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--input", str(p)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        parsed = json.loads(result.stdout)
        self.assertIn("axes", parsed)

    def test_missing_file(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--input", "/nonexistent/path.jsonl"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
