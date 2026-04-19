"""Claude Code adapter — parses `~/.claude/projects/*/<session>.jsonl`.

Entry points:
  - load_session(path, config) -> dict : aggregated session data consumed by agent_dashcam_score
  - iter_events(path)          -> Iterator[CanonicalEvent] : canonical event stream
  - iter_records(path, ...)    -> (records, partial) : raw JSONL records (kept for tests/debugging)

This module owns all Claude Code JSONL shape knowledge. The scorer only consumes
the aggregated dict from load_session, so adding Codex/Gemini adapters in later
phases requires no scorer changes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

# canonical lives beside this package: scripts/canonical.py
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from canonical import CanonicalEvent, canonicalize_tool_name  # noqa: E402


PROVIDER = "claude"


def safe_get(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def tail_lines(path: Path, n: int) -> list[str]:
    """Return last n lines of a file without loading the whole file."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        block_size = 65536
        data = b""
        while f.tell() > 0 and data.count(b"\n") <= n:
            read_size = min(block_size, f.tell())
            f.seek(-read_size, os.SEEK_CUR)
            data = f.read(read_size) + data
            f.seek(-read_size, os.SEEK_CUR)
        lines = data.splitlines()
        tail = lines[-n:] if len(lines) > n else lines
        return [ln.decode("utf-8", errors="replace") for ln in tail]


def iter_records(path: Path, tail_threshold_bytes: int, tail_n: int) -> tuple[list[dict], bool]:
    """Load JSONL records. If file exceeds threshold, return only last N lines.

    Returns (records, partial_flag).
    """
    size = path.stat().st_size
    partial = size > tail_threshold_bytes

    records: list[dict] = []
    if partial:
        source = tail_lines(path, tail_n)
    else:
        with open(path, encoding="utf-8", errors="replace") as f:
            source = list(f)

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records, partial


def extract_tool_uses(assistant_msgs: list[dict]) -> list[str]:
    names: list[str] = []
    for msg in assistant_msgs:
        content = safe_get(msg, "message", "content", default=[]) or []
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                names.append(block.get("name") or "")
    return names


def extract_tool_uses_with_input(assistant_msgs: list[dict]) -> list[tuple[str, dict]]:
    """tool_use 블록의 (name, input) 튜플 — Bash 커맨드 본문 분석용."""
    out: list[tuple[str, dict]] = []
    for msg in assistant_msgs:
        content = safe_get(msg, "message", "content", default=[]) or []
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name") or ""
                inp = block.get("input") or {}
                if not isinstance(inp, dict):
                    inp = {}
                out.append((name, inp))
    return out


def extract_assistant_text(assistant_msgs: list[dict]) -> str:
    """assistant 메시지의 text 블록을 합친 lowercase 문자열."""
    buf: list[str] = []
    for msg in assistant_msgs:
        content = safe_get(msg, "message", "content", default=[]) or []
        if isinstance(content, str):
            buf.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if isinstance(t, str):
                    buf.append(t)
    return " ".join(buf).lower()


def extract_user_text(user_msgs: list[dict]) -> str:
    """user 메시지 중 tool_result가 아닌 실제 사용자 입력만 합친 lowercase."""
    buf: list[str] = []
    for msg in user_msgs:
        content = safe_get(msg, "message", "content", default=[]) or []
        if isinstance(content, str):
            buf.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                continue
            if btype == "text":
                t = block.get("text") or ""
                if isinstance(t, str):
                    buf.append(t)
    return " ".join(buf).lower()


def load_session(path: Path, config: dict) -> dict:
    """Parse a Claude Code session JSONL into the shape agent_dashcam_score consumes.

    Returned keys:
      records, partial, assistant_msgs, user_msgs, progress_msgs, system_msgs,
      tool_names, tool_uses_with_input, assistant_text_lc, user_text_lc,
      session_id, project_dir, jsonl_lines, jsonl_bytes, provider.
    """
    threshold = config.get("jsonl_tail_threshold_mb", 20) * 1024 * 1024
    tail_n = config.get("jsonl_tail_lines", 5000)
    records, partial = iter_records(path, threshold, tail_n)

    assistant_msgs = [r for r in records if r.get("type") == "assistant"]
    user_msgs = [r for r in records if r.get("type") == "user"]
    progress_msgs = [r for r in records if r.get("type") == "progress"]
    system_msgs = [r for r in records if r.get("type") == "system"]

    tool_names = extract_tool_uses(assistant_msgs)
    tool_uses_with_input = extract_tool_uses_with_input(assistant_msgs)

    session_id: str | None = None
    project_dir: str | None = None
    for r in records:
        sid = r.get("sessionId")
        cwd = r.get("cwd")
        if sid and not session_id:
            session_id = sid
        if cwd and not project_dir:
            project_dir = cwd
        if session_id and project_dir:
            break

    return {
        "provider": PROVIDER,
        "records": records,
        "partial": partial,
        "assistant_msgs": assistant_msgs,
        "user_msgs": user_msgs,
        "progress_msgs": progress_msgs,
        "system_msgs": system_msgs,
        "tool_names": tool_names,
        "tool_uses_with_input": tool_uses_with_input,
        "assistant_text_lc": extract_assistant_text(assistant_msgs),
        "user_text_lc": extract_user_text(user_msgs),
        "session_id": session_id,
        "project_dir": project_dir,
        "jsonl_lines": len(records),
        "jsonl_bytes": path.stat().st_size,
    }


def _record_ts(r: dict) -> str:
    return r.get("timestamp") or safe_get(r, "message", "timestamp") or ""


def _record_session_id(r: dict) -> str:
    return r.get("sessionId") or ""


def iter_events(path: Path, tail_threshold_bytes: int = 20 * 1024 * 1024, tail_n: int = 5000) -> Iterator[CanonicalEvent]:
    """Yield CanonicalEvent from a Claude Code session JSONL.

    Tokens and model are attached to agent_message events (which is where Claude
    reports usage). tool_call events carry tool_name + tool_family + tool_input.
    """
    records, _ = iter_records(path, tail_threshold_bytes, tail_n)
    for r in records:
        rtype = r.get("type")
        sid = _record_session_id(r)
        ts = _record_ts(r)

        if rtype == "user":
            content = safe_get(r, "message", "content", default=[])
            text_buf: list[str] = []
            tool_results: list[dict] = []
            if isinstance(content, str):
                text_buf.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        text_buf.append(block["text"])
                    elif block.get("type") == "tool_result":
                        tool_results.append(block)
            if text_buf:
                yield CanonicalEvent(
                    ts=ts, session_id=sid, provider=PROVIDER,
                    kind="user_message", role="user",
                    text=" ".join(text_buf), raw=r,
                )
            for tr in tool_results:
                yield CanonicalEvent(
                    ts=ts, session_id=sid, provider=PROVIDER,
                    kind="tool_result", role="tool",
                    text=str(tr.get("content") or ""),
                    tool_input={"tool_use_id": tr.get("tool_use_id")},
                    raw=r,
                )

        elif rtype == "assistant":
            message = r.get("message") or {}
            usage = message.get("usage") or {}
            model = message.get("model")
            tokens_in = usage.get("input_tokens")
            tokens_out = usage.get("output_tokens")
            tokens_cr = usage.get("cache_read_input_tokens")
            tokens_cc = usage.get("cache_creation_input_tokens")

            content = message.get("content") or []
            text_buf: list[str] = []
            tool_calls: list[dict] = []
            if isinstance(content, str):
                text_buf.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        text_buf.append(block["text"])
                    elif block.get("type") == "tool_use":
                        tool_calls.append(block)

            if text_buf or tokens_in is not None or tokens_out is not None:
                yield CanonicalEvent(
                    ts=ts, session_id=sid, provider=PROVIDER,
                    kind="agent_message", role="agent",
                    text=" ".join(text_buf) if text_buf else None,
                    tokens_input=tokens_in,
                    tokens_output=tokens_out,
                    tokens_cache_read=tokens_cr,
                    tokens_cache_write=tokens_cc,
                    model=model,
                    raw=r,
                )
            for tc in tool_calls:
                name = tc.get("name") or ""
                yield CanonicalEvent(
                    ts=ts, session_id=sid, provider=PROVIDER,
                    kind="tool_call", role="agent",
                    tool_name=name,
                    tool_family=canonicalize_tool_name(name),
                    tool_input=tc.get("input") if isinstance(tc.get("input"), dict) else None,
                    raw=r,
                )

        elif rtype == "system":
            yield CanonicalEvent(
                ts=ts, session_id=sid, provider=PROVIDER,
                kind="system", role="system",
                text=str(safe_get(r, "message", "content") or r.get("content") or ""),
                raw=r,
            )

        elif rtype == "progress":
            yield CanonicalEvent(
                ts=ts, session_id=sid, provider=PROVIDER,
                kind="progress",
                text=str(safe_get(r, "data", "type") or ""),
                raw=r,
            )
