#!/usr/bin/env python3
"""Tests for OpenAI + Gemini pricing entries in config.example.json.

Covers:
  - All required model keys present.
  - Each entry has input / output / cache_read / cache_write in USD per 1M tokens.
  - compute_cost looks up the right rate and produces non-zero total_usd.
  - score_jsonl with a synthetic session of each new model produces a
    non-neutral cost_efficiency axis (proves the rate was actually used).
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


REQUIRED_MODELS = (
    "gpt-5-codex", "gpt-5", "o1-mini", "o4-mini",
    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-1.5-pro", "gemini-1.5-flash",
)


def _config():
    with open(AGENT_DASHCAM_ROOT / "config.example.json") as f:
        return json.load(f)


class TestPricingTable(unittest.TestCase):
    def test_all_models_present(self):
        rates = _config()["model_rates"]
        missing = [m for m in REQUIRED_MODELS if m not in rates]
        self.assertEqual(missing, [], f"missing pricing: {missing}")

    def test_each_entry_has_required_keys(self):
        rates = _config()["model_rates"]
        for m in REQUIRED_MODELS:
            with self.subTest(model=m):
                r = rates[m]
                for key in ("input", "output", "cache_read", "cache_write"):
                    self.assertIn(key, r, f"{m}.{key} missing")
                    self.assertIsInstance(r[key], (int, float), f"{m}.{key} not numeric")
                    self.assertGreater(r[key], 0, f"{m}.{key} must be > 0 USD/1M")

    def test_pricing_sources_documented(self):
        cfg = _config()
        self.assertIn("pricing_sources", cfg)
        self.assertIn("openai", cfg["pricing_sources"])
        self.assertIn("gemini", cfg["pricing_sources"])


def _assistant_msg(model: str, inp: int, out: int, cr: int = 0, cc: int = 0) -> dict:
    return {
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
            },
            "content": [{"type": "text", "text": "ok"}],
        }
    }


class TestComputeCost(unittest.TestCase):
    def test_openai_rate_looked_up(self):
        rates = _config()["model_rates"]
        usd, out = agent_dashcam_score.compute_cost(
            [_assistant_msg("gpt-5-codex", 1_000_000, 1_000_000)],
            rates,
        )
        expected = 1 * rates["gpt-5-codex"]["input"] + 1 * rates["gpt-5-codex"]["output"]
        self.assertAlmostEqual(usd, expected, places=4)
        self.assertEqual(out, 1_000_000)

    def test_gemini_rate_looked_up(self):
        rates = _config()["model_rates"]
        usd, _ = agent_dashcam_score.compute_cost(
            [_assistant_msg("gemini-2.5-pro", 2_000_000, 500_000, cr=100_000)],
            rates,
        )
        r = rates["gemini-2.5-pro"]
        expected = 2 * r["input"] + 0.5 * r["output"] + 0.1 * r["cache_read"]
        self.assertAlmostEqual(usd, expected, places=4)

    def test_unknown_model_uses_default(self):
        rates = _config()["model_rates"]
        usd, _ = agent_dashcam_score.compute_cost(
            [_assistant_msg("never-shipped-model", 1_000_000, 0)],
            rates,
        )
        self.assertAlmostEqual(usd, rates["default"]["input"], places=4)


def _write(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _claude_shape_session(model: str, out_tokens: int) -> list[dict]:
    """Synthetic Claude-shaped session (because cost accounting is model-agnostic
    once the adapter returns Claude-shape usage dicts)."""
    return [
        {"type": "user", "sessionId": f"price-{model}",
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "sessionId": f"price-{model}",
         "message": {"role": "assistant", "model": model,
                     "usage": {"input_tokens": 10_000,
                               "output_tokens": out_tokens,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0},
                     "content": [{"type": "text", "text": "answer"},
                                 {"type": "tool_use", "id": "t1", "name": "Read",
                                  "input": {"file_path": "README.md"}}]}},
    ]


class TestScoreCostsWithNewModels(unittest.TestCase):
    def test_gpt5_codex_non_zero_cost(self):
        config = _config()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "gpt5.jsonl"
            _write(p, _claude_shape_session("gpt-5-codex", out_tokens=10_000))
            result = agent_dashcam_score.score_jsonl(p, config, provider="claude")
            self.assertGreater(result["meta"]["total_usd"], 0.0)
            # cost_efficiency defaults to 0.5 only when total_usd==0 AND total_output==0.
            # Here we have real output + real cost, so the axis must be computed.
            self.assertNotEqual(result["axes"]["cost_efficiency"], 0.5)

    def test_gemini25_pro_non_zero_cost(self):
        config = _config()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "gemini25.jsonl"
            _write(p, _claude_shape_session("gemini-2.5-pro", out_tokens=5_000))
            result = agent_dashcam_score.score_jsonl(p, config, provider="claude")
            self.assertGreater(result["meta"]["total_usd"], 0.0)
            self.assertNotEqual(result["axes"]["cost_efficiency"], 0.5)


if __name__ == "__main__":
    unittest.main()
