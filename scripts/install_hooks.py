#!/usr/bin/env python3
"""Agent Dashcam hooks installer — settings.json에 SessionStart/Stop 항목 append.

안전장치:
  - symlink dereference (Path.resolve())하여 실제 파일에 쓰기
  - 수정 전 {resolved_path}.bak.{YYYYMMDD-HHMMSS} 백업 생성
  - 기존 hook entry 제거 금지
  - idempotent: 이미 agent-dashcam hook이 등록돼 있으면 skip

사용법:
  python3 install_hooks.py              # 적용
  python3 install_hooks.py --dry-run    # 시뮬레이션
  python3 install_hooks.py --uninstall  # 제거
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


SETTINGS_LINK = Path.home() / ".claude" / "settings.json"
AGENT_DASHCAM_START = '"$HOME/.claude/agent-dashcam/hooks/session-start.mjs"'
AGENT_DASHCAM_STOP = '"$HOME/.claude/agent-dashcam/hooks/session-stop.mjs"'
START_CMD = f"node {AGENT_DASHCAM_START}"
STOP_CMD = f"node {AGENT_DASHCAM_STOP}"


def resolve_settings_path() -> Path:
    real = SETTINGS_LINK.resolve()
    if not real.exists():
        raise SystemExit(f"settings.json not found at resolved path: {real}")
    return real


def backup(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = path.with_suffix(path.suffix + f".bak.{ts}")
    shutil.copy2(path, dst)
    return dst


def already_has(entries: list, command: str) -> bool:
    for entry in entries:
        for h in entry.get("hooks", []) or []:
            if h.get("type") == "command" and h.get("command") == command:
                return True
    return False


def make_entry(command: str) -> dict:
    return {"hooks": [{"type": "command", "command": command}]}


def install(data: dict) -> dict:
    hooks = data.setdefault("hooks", {})
    for key, cmd in (("SessionStart", START_CMD), ("Stop", STOP_CMD)):
        arr = hooks.setdefault(key, [])
        if already_has(arr, cmd):
            print(f"[skip] {key}: already installed")
            continue
        arr.append(make_entry(cmd))
        print(f"[add]  {key}: {cmd}")
    return data


def uninstall(data: dict) -> dict:
    hooks = data.get("hooks", {})
    for key, cmd in (("SessionStart", START_CMD), ("Stop", STOP_CMD)):
        arr = hooks.get(key, [])
        keep = []
        removed = 0
        for entry in arr:
            sub = [h for h in entry.get("hooks", []) or [] if not (h.get("type") == "command" and h.get("command") == cmd)]
            if not sub:
                removed += 1
                continue
            entry["hooks"] = sub
            keep.append(entry)
        if removed:
            hooks[key] = keep
            print(f"[remove] {key}: removed {removed} agent-dashcam entries")
        else:
            print(f"[skip]   {key}: no agent-dashcam entries found")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Dashcam hooks installer")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()

    real = resolve_settings_path()
    print(f"resolved settings: {real}")
    if SETTINGS_LINK.is_symlink():
        print(f"symlink: {SETTINGS_LINK} → {real}")

    with open(real) as f:
        data = json.load(f)

    if args.uninstall:
        new_data = uninstall(data)
    else:
        new_data = install(data)

    if args.dry_run:
        print("--- dry-run diff ---")
        print(json.dumps(new_data.get("hooks", {}), indent=2)[:2000])
        return 0

    bak = backup(real)
    print(f"backup: {bak}")

    with open(real, "w") as f:
        json.dump(new_data, f, indent=2)
        f.write("\n")
    print(f"updated: {real}")

    if SETTINGS_LINK.is_symlink():
        if SETTINGS_LINK.resolve() == real:
            print("symlink preserved (still points to resolved path)")
        else:
            print("WARN: symlink changed unexpectedly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
