"""Gemini CLI adapter — parses `~/.gemini/tmp/<project_hash>/chats/session-<uuid>.json`.

Entry points:
  - load_session(path, config) -> dict : aggregated session data consumed by agent_dashcam_score
  - iter_events(path)          -> Iterator[CanonicalEvent] : canonical event stream
  - iter_records(path, ...)    -> (records, partial) : raw JSONL records (kept for tests/debugging)

Despite the `.json` extension, Gemini CLI session files are JSONL (one JSON object per line).
Each line is either:
  - a regular event with `role` ("user" | "model" | "system" | "tool") and `parts` array,
  - or a control marker: `$rewindTo` (transcript rewind anchor) or `$set` (field mutation).

The scorer (Phase 1) still consumes the Claude-shaped dict keys from `load_session`, so this
adapter re-projects Gemini events into Claude-shaped msg dicts internally. Gemini's
`functionCall` parts become assistant `tool_use` content blocks, and `functionResponse`
parts are re-emitted as user messages with `tool_result` content blocks (mirroring how
Claude Code threads tool results back through the user role).

Limitations:
  - `$rewindTo` and `$set` markers are tolerated — they are bucketed into `progress_msgs`
    so they do not crash the parser or inflate user/assistant counts, but the adapter does
    NOT truncate preceding content. If the user rewinds past N turns, those N turns still
    contribute to token / tool-use totals. Proper rewind-aware truncation is a Phase 4+
    concern and requires stable event ids that current Gemini CLI lines rarely carry.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterator

# canonical lives beside this package: scripts/canonical.py
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from canonical import CanonicalEvent, canonicalize_tool_name  # noqa: E402


PROVIDER = "gemini"

_SESSION_ID_RE = re.compile(r"^session-(?P<uuid>.+)$")


def _safe_size(path: Path) -> int:
    """stat().st_size with best-effort fallback when the file is gone."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


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

    Returns (records, partial_flag). Malformed lines are silently skipped.
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
            obj = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records, partial


def _extract_session_id_from_filename(path: Path) -> str | None:
    """Gemini CLI embeds the session id in the filename (`session-<uuid>.json`)."""
    m = _SESSION_ID_RE.match(path.stem)
    if m:
        return m.group("uuid")
    return None


def _project_from_set(records: list[dict]) -> str | None:
    """Scan for `$set` markers that set `metadata.cwd` and return the latest value."""
    project_dir: str | None = None
    for r in records:
        if "$set" not in r:
            continue
        payload = r.get("$set")
        if not isinstance(payload, dict):
            continue
        if payload.get("path") == "metadata.cwd":
            val = payload.get("value")
            if isinstance(val, str) and val:
                project_dir = val
    return project_dir


def _record_ts(r: dict) -> str:
    return safe_get(r, "metadata", "timestamp", default="") or r.get("timestamp") or ""


def _parts(r: dict) -> list[dict]:
    parts = r.get("parts")
    if not isinstance(parts, list):
        return []
    return [p for p in parts if isinstance(p, dict)]


def _project_to_claude_msgs(records: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Re-project Gemini events into Claude-shaped msg dicts.

    Returns (assistant_msgs, user_msgs, progress_msgs, system_msgs).

    Mapping:
      - role == "user"   -> Claude `user` msg with text content blocks.
      - role == "model"  -> Claude `assistant` msg with `text` / `tool_use` blocks, plus
                            any `functionResponse` parts split off into a trailing
                            `user` msg with `tool_result` blocks (mirroring Claude's
                            tool-result-through-user-role convention).
      - role == "system" -> Claude `system` msg with text content.
      - role == "tool"   -> Claude `user` msg with `tool_result` content (tools in Gemini
                            sometimes appear with an explicit `tool` role).
      - `$rewindTo`/`$set` markers -> progress-like entries; not surfaced as msgs.
    """
    assistant_msgs: list[dict] = []
    user_msgs: list[dict] = []
    progress_msgs: list[dict] = []
    system_msgs: list[dict] = []

    for r in records:
        # Control markers — kept as progress_msgs so downstream counts stay sane.
        if "$rewindTo" in r or "$set" in r:
            progress_msgs.append(r)
            continue

        role = r.get("role")
        parts = _parts(r)

        if role == "user":
            content: list[dict] = []
            for p in parts:
                text = p.get("text")
                if isinstance(text, str) and text:
                    content.append({"type": "text", "text": text})
            if content:
                user_msgs.append({
                    "type": "user",
                    "message": {"content": content},
                })

        elif role == "model":
            content: list[dict] = []
            tool_results: list[dict] = []
            for p in parts:
                text = p.get("text")
                if isinstance(text, str) and text:
                    content.append({"type": "text", "text": text})
                fc = p.get("functionCall")
                if isinstance(fc, dict):
                    args = fc.get("args")
                    if not isinstance(args, dict):
                        args = {}
                    content.append({
                        "type": "tool_use",
                        "name": fc.get("name") or "",
                        "input": args,
                    })
                fr = p.get("functionResponse")
                if isinstance(fr, dict):
                    response = fr.get("response")
                    is_error = False
                    out_text = ""
                    if isinstance(response, dict):
                        err = response.get("error")
                        if err not in (None, "", False):
                            is_error = True
                        out = response.get("output")
                        if isinstance(out, str):
                            out_text = out
                        elif out is not None:
                            out_text = json.dumps(out, ensure_ascii=False)
                        else:
                            out_text = json.dumps(response, ensure_ascii=False)
                    else:
                        out_text = str(response) if response is not None else ""
                    tool_results.append({
                        "type": "tool_result",
                        "is_error": is_error,
                        "content": out_text,
                    })

            usage_meta = r.get("usageMetadata") or {}
            usage = {
                "input_tokens": usage_meta.get("promptTokenCount", 0) or 0,
                "output_tokens": usage_meta.get("candidatesTokenCount", 0) or 0,
                "cache_read_input_tokens": usage_meta.get("cachedContentTokenCount", 0) or 0,
                "cache_creation_input_tokens": 0,
            }
            model = safe_get(r, "metadata", "model", default=None) or r.get("model")
            assistant_msgs.append({
                "type": "assistant",
                "message": {
                    "model": model,
                    "usage": usage,
                    "content": content,
                },
            })
            if tool_results:
                user_msgs.append({
                    "type": "user",
                    "message": {"content": tool_results},
                })

        elif role == "tool":
            # Explicit tool role — treat as user tool_result carrier.
            content = []
            for p in parts:
                fr = p.get("functionResponse")
                if isinstance(fr, dict):
                    response = fr.get("response")
                    is_error = False
                    out_text = ""
                    if isinstance(response, dict):
                        err = response.get("error")
                        if err not in (None, "", False):
                            is_error = True
                        out = response.get("output")
                        if isinstance(out, str):
                            out_text = out
                        elif out is not None:
                            out_text = json.dumps(out, ensure_ascii=False)
                        else:
                            out_text = json.dumps(response, ensure_ascii=False)
                    content.append({
                        "type": "tool_result",
                        "is_error": is_error,
                        "content": out_text,
                    })
            if content:
                user_msgs.append({
                    "type": "user",
                    "message": {"content": content},
                })

        elif role == "system":
            text_buf: list[str] = []
            for p in parts:
                text = p.get("text")
                if isinstance(text, str):
                    text_buf.append(text)
            system_msgs.append({
                "type": "system",
                "message": {"content": " ".join(text_buf)},
            })
        # Unknown roles are dropped.

    return assistant_msgs, user_msgs, progress_msgs, system_msgs


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
    """Parse a Gemini CLI session JSONL into the shape agent_dashcam_score consumes.

    Returned keys (identical to the Claude adapter surface):
      provider, records, partial, assistant_msgs, user_msgs, progress_msgs, system_msgs,
      tool_names, tool_uses_with_input, assistant_text_lc, user_text_lc,
      session_id, project_dir, jsonl_lines, jsonl_bytes.
    """
    threshold = config.get("jsonl_tail_threshold_mb", 20) * 1024 * 1024
    tail_n = config.get("jsonl_tail_lines", 5000)
    records, partial = iter_records(path, threshold, tail_n)

    assistant_msgs, user_msgs, progress_msgs, system_msgs = _project_to_claude_msgs(records)
    tool_names = extract_tool_uses(assistant_msgs)
    tool_uses_with_input = extract_tool_uses_with_input(assistant_msgs)

    session_id = _extract_session_id_from_filename(path)
    project_dir = _project_from_set(records)
    # Fallback: look for a top-level cwd / metadata.cwd on regular events.
    if not project_dir:
        for r in records:
            cwd = safe_get(r, "metadata", "cwd")
            if isinstance(cwd, str) and cwd:
                project_dir = cwd
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
        "jsonl_bytes": _safe_size(path),
    }


def iter_events(path: Path, tail_threshold_bytes: int = 20 * 1024 * 1024, tail_n: int = 5000) -> Iterator[CanonicalEvent]:
    """Yield CanonicalEvent from a Gemini CLI session JSONL.

    Emits:
      - user_message   for `role == "user"` parts with text
      - agent_message  for `role == "model"` text + usageMetadata
      - tool_call      one per `functionCall` part (raw snake_case name + canonical family)
      - tool_result    one per `functionResponse` part (from model or tool roles)
      - system         for `role == "system"` events
      - progress       for `$rewindTo` / `$set` control markers
    """
    records, _ = iter_records(path, tail_threshold_bytes, tail_n)
    session_id = _extract_session_id_from_filename(path) or ""

    for r in records:
        ts = _record_ts(r)

        if "$rewindTo" in r or "$set" in r:
            yield CanonicalEvent(
                ts=ts, session_id=session_id, provider=PROVIDER,
                kind="progress",
                text="rewindTo" if "$rewindTo" in r else "set",
                raw=r,
            )
            continue

        role = r.get("role")
        parts = _parts(r)

        if role == "user":
            text_buf: list[str] = []
            for p in parts:
                t = p.get("text")
                if isinstance(t, str) and t:
                    text_buf.append(t)
            if text_buf:
                yield CanonicalEvent(
                    ts=ts, session_id=session_id, provider=PROVIDER,
                    kind="user_message", role="user",
                    text=" ".join(text_buf), raw=r,
                )

        elif role == "model":
            text_buf: list[str] = []
            tool_calls: list[dict] = []
            tool_results: list[dict] = []
            for p in parts:
                t = p.get("text")
                if isinstance(t, str) and t:
                    text_buf.append(t)
                fc = p.get("functionCall")
                if isinstance(fc, dict):
                    tool_calls.append(fc)
                fr = p.get("functionResponse")
                if isinstance(fr, dict):
                    tool_results.append(fr)

            usage_meta = r.get("usageMetadata") or {}
            tokens_in = usage_meta.get("promptTokenCount")
            tokens_out = usage_meta.get("candidatesTokenCount")
            tokens_cr = usage_meta.get("cachedContentTokenCount")
            model = safe_get(r, "metadata", "model", default=None) or r.get("model")

            if text_buf or tokens_in is not None or tokens_out is not None:
                yield CanonicalEvent(
                    ts=ts, session_id=session_id, provider=PROVIDER,
                    kind="agent_message", role="agent",
                    text=" ".join(text_buf) if text_buf else None,
                    tokens_input=tokens_in,
                    tokens_output=tokens_out,
                    tokens_cache_read=tokens_cr,
                    tokens_cache_write=None,
                    model=model,
                    raw=r,
                )
            for fc in tool_calls:
                name = fc.get("name") or ""
                args = fc.get("args") if isinstance(fc.get("args"), dict) else None
                yield CanonicalEvent(
                    ts=ts, session_id=session_id, provider=PROVIDER,
                    kind="tool_call", role="agent",
                    tool_name=name,
                    tool_family=canonicalize_tool_name(name),
                    tool_input=args,
                    raw=r,
                )
            for fr in tool_results:
                response = fr.get("response")
                if isinstance(response, dict):
                    out = response.get("output")
                    text = out if isinstance(out, str) else json.dumps(response, ensure_ascii=False)
                else:
                    text = str(response) if response is not None else ""
                yield CanonicalEvent(
                    ts=ts, session_id=session_id, provider=PROVIDER,
                    kind="tool_result", role="tool",
                    text=text,
                    tool_name=fr.get("name") or None,
                    raw=r,
                )

        elif role == "tool":
            for p in parts:
                fr = p.get("functionResponse")
                if not isinstance(fr, dict):
                    continue
                response = fr.get("response")
                if isinstance(response, dict):
                    out = response.get("output")
                    text = out if isinstance(out, str) else json.dumps(response, ensure_ascii=False)
                else:
                    text = str(response) if response is not None else ""
                yield CanonicalEvent(
                    ts=ts, session_id=session_id, provider=PROVIDER,
                    kind="tool_result", role="tool",
                    text=text,
                    tool_name=fr.get("name") or None,
                    raw=r,
                )

        elif role == "system":
            text_buf = []
            for p in parts:
                t = p.get("text")
                if isinstance(t, str):
                    text_buf.append(t)
            yield CanonicalEvent(
                ts=ts, session_id=session_id, provider=PROVIDER,
                kind="system", role="system",
                text=" ".join(text_buf),
                raw=r,
            )
