#!/usr/bin/env python3
"""합성 JSONL fixture 생성 (10줄/100줄/1000줄).

unit test용 decoy 세션 로그. 실제 Claude Code JSONL 스키마를 모사.
"""
from __future__ import annotations

import json
from pathlib import Path
from random import Random

FIXTURES_DIR = Path(__file__).parent
SESSION_ID = "test-fixture-session-00000000-0000-0000-0000-000000000000"
CWD = "/Users/fixture/project"
TOOL_NAMES = ["Read", "Edit", "Bash", "Grep", "Write", "Glob"]


def gen_record(rng: Random, idx: int, total: int) -> dict:
    roll = rng.random()
    base = {
        "sessionId": SESSION_ID,
        "cwd": CWD,
        "timestamp": f"2026-04-19T{(idx // 3600) % 24:02d}:{(idx // 60) % 60:02d}:{idx % 60:02d}.000Z",
        "uuid": f"uuid-{idx:06d}",
        "version": "2.1.112",
        "gitBranch": "main",
    }
    if roll < 0.4:
        base.update(
            {
                "type": "assistant",
                "message": {
                    "model": rng.choice(["claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5"]),
                    "content": [
                        {"type": "text", "text": "stub"},
                        {
                            "type": "tool_use",
                            "id": f"toolu_{idx:06d}",
                            "name": rng.choice(TOOL_NAMES),
                            "input": {},
                        },
                    ],
                    "usage": {
                        "input_tokens": rng.randint(5, 50),
                        "cache_read_input_tokens": rng.randint(1000, 10000),
                        "cache_creation_input_tokens": rng.randint(0, 500),
                        "output_tokens": rng.randint(50, 500),
                    },
                },
            }
        )
    elif roll < 0.7:
        base.update(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"toolu_{idx:06d}",
                            "is_error": rng.random() < 0.1,
                            "content": "stub result",
                        }
                    ],
                },
            }
        )
    elif roll < 0.9:
        base.update(
            {
                "type": "progress",
                "data": {
                    "type": "hook_progress",
                    "hookEvent": rng.choice(["PreToolUse", "PostToolUse", "SessionStart"]),
                    "hookName": "test-hook",
                    "command": "echo stub",
                },
                "toolUseID": f"toolu_{idx:06d}",
            }
        )
    else:
        base.update(
            {
                "type": "system",
                "subtype": rng.choice(["turn_duration", "stop_hook_summary", "compact_boundary"]),
                "content": "stub",
                "level": "info",
            }
        )
    return base


def make_fixture(n_lines: int, seed: int) -> Path:
    rng = Random(seed)
    out = FIXTURES_DIR / f"sample_{n_lines:04d}.jsonl"
    with open(out, "w") as f:
        for i in range(n_lines):
            f.write(json.dumps(gen_record(rng, i, n_lines)) + "\n")
    return out


def main() -> None:
    for n, seed in [(10, 1), (100, 2), (1000, 3)]:
        p = make_fixture(n, seed)
        print(f"created {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
