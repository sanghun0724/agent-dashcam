#!/usr/bin/env python3
"""Smoke tests for hooks/codex-stop.mjs and hooks/gemini-stop.mjs.

Covers:
  - node --check passes (syntax gate)
  - dry-run with a fixture transcript_path on stdin prints the resolved path
    and does not throw
  - The resolved transcript feeds back into scripts/agent_dashcam_score.py with the
    right --provider flag (executed separately for speed)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

AGENT_DASHCAM_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = AGENT_DASHCAM_ROOT / "hooks"


def _write(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _codex_fixture() -> list[dict]:
    return [
        {"timestamp": "2026-04-19T00:00:00.000Z", "type": "session_meta",
         "payload": {"id": "codex-smoke", "timestamp": "2026-04-19T00:00:00.000Z",
                     "cwd": "/tmp", "originator": "codex_cli",
                     "model": "gpt-5-codex", "instructions": None}},
        {"timestamp": "2026-04-19T00:00:01.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "ok"}]}},
    ]


def _gemini_fixture() -> list[dict]:
    return [
        {"role": "user", "parts": [{"text": "hi"}],
         "metadata": {"timestamp": "2026-04-19T00:00:00Z"}},
        {"role": "model", "parts": [{"text": "ok"}],
         "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1,
                           "totalTokenCount": 6, "cachedContentTokenCount": 0},
         "metadata": {"timestamp": "2026-04-19T00:00:01Z", "model": "gemini-2.5-pro"}},
    ]


def _have_node() -> bool:
    try:
        subprocess.run(["node", "--version"], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


@unittest.skipUnless(_have_node(), "node not available in test environment")
class TestHookSyntax(unittest.TestCase):
    def test_codex_hook_syntax(self):
        subprocess.run(["node", "--check", str(HOOKS_DIR / "codex-stop.mjs")], check=True)

    def test_gemini_hook_syntax(self):
        subprocess.run(["node", "--check", str(HOOKS_DIR / "gemini-stop.mjs")], check=True)


@unittest.skipUnless(_have_node(), "node not available in test environment")
class TestHookDryRun(unittest.TestCase):
    def _run(self, hook_name: str, payload: dict, extra_root: Path | None = None) -> dict:
        env = dict(os.environ)
        if extra_root is not None:
            env["AGENT_DASHCAM_HOOK_EXTRA_ROOTS"] = str(extra_root)
        out = subprocess.run(
            ["node", str(HOOKS_DIR / hook_name), "--dry-run"],
            input=json.dumps(payload),
            check=True, capture_output=True, text=True, timeout=10, env=env,
        )
        first = out.stdout.splitlines()[0].strip()
        return json.loads(first)

    def test_codex_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "rollout-codex-smoke.jsonl"
            _write(p, _codex_fixture())
            payload = {"transcript_path": str(p), "session_id": "codex-smoke"}
            result = self._run("codex-stop.mjs", payload, extra_root=Path(tmp))
            self.assertTrue(result.get("dry_run"))
            self.assertEqual(result["hook"], "codex-stop")
            self.assertEqual(result["rollout"], str(p))

    def test_gemini_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "session-gemini-smoke.json"
            _write(p, _gemini_fixture())
            payload = {"transcript_path": str(p), "session_id": "gemini-smoke"}
            result = self._run("gemini-stop.mjs", payload, extra_root=Path(tmp))
            self.assertTrue(result.get("dry_run"))
            self.assertEqual(result["hook"], "gemini-stop")
            self.assertEqual(result["transcript"], str(p))

    def test_codex_missing_payload_exits_zero(self):
        # No transcript_path, no session_id → wrapper should log and exit 0, not crash.
        proc = subprocess.run(
            ["node", str(HOOKS_DIR / "codex-stop.mjs"), "--dry-run"],
            input="{}", capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0)

    def test_gemini_missing_payload_exits_zero(self):
        proc = subprocess.run(
            ["node", str(HOOKS_DIR / "gemini-stop.mjs"), "--dry-run"],
            input="{}", capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0)


@unittest.skipUnless(_have_node(), "node not available in test environment")
class TestHookPathAllowlist(unittest.TestCase):
    """Security: stdin transcript_path outside the allowlist must not route
    the scorer to arbitrary filesystem locations. Without extra_root set,
    a tempdir path is outside (~/.codex / ~/.gemini / ~/.claude / $AGENT_DASHCAM_ROOT)
    and the hook should exit 0 without resolving."""

    def test_codex_rejects_path_outside_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "rollout-evil.jsonl"
            _write(p, _codex_fixture())
            # No AGENT_DASHCAM_HOOK_EXTRA_ROOTS → tempdir is outside allowlist.
            env = {k: v for k, v in os.environ.items()
                   if k != "AGENT_DASHCAM_HOOK_EXTRA_ROOTS"}
            payload = {"transcript_path": str(p), "session_id": "does-not-exist"}
            proc = subprocess.run(
                ["node", str(HOOKS_DIR / "codex-stop.mjs"), "--dry-run"],
                input=json.dumps(payload), capture_output=True, text=True,
                timeout=10, env=env,
            )
            self.assertEqual(proc.returncode, 0)
            # Dry-run emits JSON only when a path resolves. Rejected path → no JSON line.
            self.assertFalse(proc.stdout.strip(),
                             f"expected empty stdout, got: {proc.stdout!r}")

    def test_gemini_rejects_path_outside_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "session-evil.json"
            _write(p, _gemini_fixture())
            env = {k: v for k, v in os.environ.items()
                   if k != "AGENT_DASHCAM_HOOK_EXTRA_ROOTS"}
            payload = {"transcript_path": str(p), "session_id": "does-not-exist"}
            proc = subprocess.run(
                ["node", str(HOOKS_DIR / "gemini-stop.mjs"), "--dry-run"],
                input=json.dumps(payload), capture_output=True, text=True,
                timeout=10, env=env,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertFalse(proc.stdout.strip(),
                             f"expected empty stdout, got: {proc.stdout!r}")


@unittest.skipUnless(_have_node(), "node not available in test environment")
class TestHookScoresThroughScorer(unittest.TestCase):
    """End-to-end: codex-stop / gemini-stop without --dry-run actually invoke
    agent_dashcam_score.py with the right --provider flag. We use a bespoke
    AGENT_DASHCAM_ROOT so the save-path is a throwaway tempdir."""

    def _run_hook(self, hook_name: str, payload: dict, agent_dashcam_root: Path):
        env = dict(os.environ)
        env["AGENT_DASHCAM_ROOT"] = str(agent_dashcam_root)
        # scripts/ lives in the real repo; copy config + scripts into the sandbox root.
        scripts_src = AGENT_DASHCAM_ROOT / "scripts"
        scripts_dst = agent_dashcam_root / "scripts"
        if not scripts_dst.exists():
            scripts_dst.symlink_to(scripts_src)
        cfg_src = AGENT_DASHCAM_ROOT / "config.example.json"
        cfg_dst = agent_dashcam_root / "config.example.json"
        if not cfg_dst.exists():
            cfg_dst.symlink_to(cfg_src)
        proc = subprocess.run(
            ["node", str(HOOKS_DIR / hook_name)],
            input=json.dumps(payload), capture_output=True, text=True,
            env=env, timeout=15,
        )
        return proc

    def test_codex_hook_writes_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            (sandbox / "scores").mkdir(parents=True, exist_ok=True)
            rollout = sandbox / "rollout-codex-smoke.jsonl"
            _write(rollout, _codex_fixture())
            payload = {"transcript_path": str(rollout), "session_id": "codex-smoke"}
            proc = self._run_hook("codex-stop.mjs", payload, sandbox)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            scores = list((sandbox / "scores").glob("*.json"))
            self.assertTrue(scores, "expected at least one score JSON in sandbox")

    def test_gemini_hook_writes_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            (sandbox / "scores").mkdir(parents=True, exist_ok=True)
            transcript = sandbox / "session-gemini-smoke.json"
            _write(transcript, _gemini_fixture())
            payload = {"transcript_path": str(transcript), "session_id": "gemini-smoke"}
            proc = self._run_hook("gemini-stop.mjs", payload, sandbox)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            scores = list((sandbox / "scores").glob("*.json"))
            self.assertTrue(scores, "expected at least one score JSON in sandbox")


if __name__ == "__main__":
    unittest.main()
