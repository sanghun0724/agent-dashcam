"""Codex CLI adapter — parses `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.

Entry points (mirrors scripts/adapters/claude.py):
  - load_session(path, config) -> dict : Claude-shaped aggregated session data
  - iter_events(path)          -> Iterator[CanonicalEvent] : canonical event stream
  - iter_records(path, ...)    -> (records, partial) : raw JSONL records

Codex JSONL envelope is `{"timestamp", "type", "payload"}`. This adapter
re-projects Codex payloads into Claude-shaped msg dicts so agent_dashcam_score.py
consumes them unchanged (Phase 1 contract — see docs/ARCHITECTURE.md §7).

Known payload types handled:
  - session_meta         -> session_id + project_dir + default model
  - response_item        -> assistant msg (message/function_call/local_shell_call/
                            web_search_call/reasoning) bundled until the turn flushes
  - event_msg            -> attach token_count usage to most recent assistant msg;
                            other subtypes emitted as progress records
  - function_call_output -> user msg with tool_result block
  - user message payload -> user msg
  - turn_context         -> ignored
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


PROVIDER = "codex"


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

    Returns (records, partial_flag). Malformed lines are skipped silently.
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
        except (json.JSONDecodeError, RecursionError):
            continue
    return records, partial


def _parse_arguments(raw) -> dict:
    """Codex function_call.arguments is a JSON string. Parse defensively."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _reasoning_text(payload: dict) -> str:
    """Flatten a reasoning payload's summary into a single string."""
    summary = payload.get("summary")
    if isinstance(summary, str):
        return summary
    if isinstance(summary, list):
        parts: list[str] = []
        for item in summary:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                txt = item.get("text") or item.get("content")
                if isinstance(txt, str):
                    parts.append(txt)
        return " ".join(parts)
    return ""


def _local_shell_command(payload: dict) -> str:
    """Join local_shell_call.action.command list into a single shell string."""
    action = payload.get("action") or {}
    cmd = action.get("command") if isinstance(action, dict) else None
    if isinstance(cmd, list):
        return " ".join(str(c) for c in cmd)
    if isinstance(cmd, str):
        return cmd
    return ""


def _tool_result_is_error(output) -> bool:
    if not isinstance(output, str):
        return False
    lower = output.lower()
    return lower.startswith("error:") or '"error"' in lower or "\nerror:" in lower


def _new_assistant_msg(model: str | None) -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "content": [],
            "usage": {},
        },
    }


def _flush_assistant(buffer: list[dict], out: list[dict]) -> None:
    """Commit the in-progress assistant message if it has any content."""
    if buffer and buffer[0]["message"]["content"]:
        out.append(buffer[0])
    buffer.clear()


def _convert_response_item(payload: dict) -> list[dict]:
    """Convert one response_item payload into Claude-style content blocks."""
    blocks: list[dict] = []
    rtype = payload.get("type")

    if rtype == "message":
        role = payload.get("role")
        if role != "assistant":
            return []
        content = payload.get("content") or []
        if isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                ctype = c.get("type")
                text = c.get("text")
                if ctype in ("output_text", "text") and isinstance(text, str):
                    blocks.append({"type": "text", "text": text})

    elif rtype == "function_call":
        name = payload.get("name") or ""
        input_dict = _parse_arguments(payload.get("arguments"))
        blocks.append({
            "type": "tool_use",
            "id": payload.get("call_id") or "",
            "name": name,
            "input": input_dict,
        })

    elif rtype == "local_shell_call":
        blocks.append({
            "type": "tool_use",
            "id": payload.get("call_id") or "",
            "name": "local_shell_call",
            "input": {"command": _local_shell_command(payload)},
        })

    elif rtype == "web_search_call":
        query = payload.get("query")
        inp: dict = {}
        if isinstance(query, str):
            inp["query"] = query
        blocks.append({
            "type": "tool_use",
            "id": payload.get("call_id") or "",
            "name": "web_search_call",
            "input": inp,
        })

    elif rtype == "reasoning":
        text = _reasoning_text(payload)
        if text:
            blocks.append({"type": "text", "text": text})

    return blocks


def _convert_user_message_payload(payload: dict) -> dict | None:
    """Map a Codex user message payload to a Claude-shaped user msg."""
    if payload.get("role") != "user":
        return None
    content_blocks: list[dict] = []
    content = payload.get("content") or []
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            ctype = c.get("type")
            text = c.get("text")
            if ctype in ("input_text", "text", "output_text") and isinstance(text, str):
                content_blocks.append({"type": "text", "text": text})
    if not content_blocks:
        return None
    return {"type": "user", "message": {"content": content_blocks}}


def _convert_function_call_output(payload: dict) -> dict:
    """Map function_call_output payload to a Claude user-tool_result msg."""
    output = payload.get("output") or ""
    if not isinstance(output, str):
        try:
            output = json.dumps(output)
        except (TypeError, ValueError):
            output = str(output)
    return {
        "type": "user",
        "message": {
            "content": [{
                "type": "tool_result",
                "tool_use_id": payload.get("call_id") or "",
                "content": output,
                "is_error": _tool_result_is_error(output),
            }],
        },
    }


def _attach_usage(assistant_msgs: list[dict], info: dict) -> None:
    """Merge Codex token_count totals into the last assistant msg usage dict.

    Prefers `last_token_usage` (per-turn marginal) over `total_token_usage`
    (session-cumulative running total). Summing per-turn marginals across N
    turns yields the correct session total; summing cumulative snapshots from
    N `token_count` events inflates by ~N*.
    """
    if not assistant_msgs:
        return
    totals = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(totals, dict):
        totals = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(totals, dict):
        totals = info if isinstance(info, dict) else {}
    def _as_int(x) -> int:
        return int(x) if isinstance(x, (int, float)) else 0
    input_tokens = _as_int(totals.get("input_tokens"))
    cached = _as_int(totals.get("cached_input_tokens"))
    output_tokens = _as_int(totals.get("output_tokens"))
    usage = assistant_msgs[-1]["message"].setdefault("usage", {})
    # Codex `total_token_usage.input_tokens` excludes cached (validated); Claude's
    # `input_tokens` is likewise the non-cached portion, so no subtraction is
    # needed for the total-path fallback.
    # UNVERIFIED: `last_token_usage.input_tokens` cached-exclusion semantics have
    # not been validated against a real rollout sample. If Codex reports this
    # field inclusive of cached tokens, cost_efficiency will over-count by the
    # cached delta. See CHANGELOG "Known limitations" for the open question.
    usage["input_tokens"] = input_tokens
    usage["cache_read_input_tokens"] = cached
    usage["cache_creation_input_tokens"] = usage.get("cache_creation_input_tokens", 0)
    usage["output_tokens"] = output_tokens


def _build_msg_streams(records: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[dict], str | None, str | None, str | None]:
    """Walk Codex records and produce Claude-shaped msg lists.

    Returns:
      (assistant_msgs, user_msgs, progress_msgs, system_msgs,
       session_id, project_dir, default_model)
    """
    assistant_msgs: list[dict] = []
    user_msgs: list[dict] = []
    progress_msgs: list[dict] = []
    system_msgs: list[dict] = []
    session_id: str | None = None
    project_dir: str | None = None
    default_model: str | None = None

    # One-slot buffer for the currently-accumulating assistant msg.
    buf: list[dict] = []

    for rec in records:
        rtype = rec.get("type")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}

        if rtype == "session_meta":
            if not session_id:
                session_id = payload.get("id")
            if not project_dir:
                project_dir = payload.get("cwd")
            if not default_model:
                default_model = payload.get("model")
            system_msgs.append({
                "type": "system",
                "subtype": "session_meta",
                "content": json.dumps({
                    "id": session_id,
                    "cwd": project_dir,
                    "model": default_model,
                }),
            })

        elif rtype == "response_item":
            ptype = payload.get("type")
            if ptype == "message" and payload.get("role") == "user":
                # User message embedded as a response_item (rare but tolerated).
                _flush_assistant(buf, assistant_msgs)
                msg = _convert_user_message_payload(payload)
                if msg:
                    user_msgs.append(msg)
            else:
                blocks = _convert_response_item(payload)
                if not blocks:
                    continue
                if not buf:
                    buf.append(_new_assistant_msg(default_model))
                buf[0]["message"]["content"].extend(blocks)

        elif rtype == "function_call_output":
            _flush_assistant(buf, assistant_msgs)
            user_msgs.append(_convert_function_call_output(payload))

        elif rtype == "event_msg":
            ptype = payload.get("type")
            if ptype == "token_count":
                info = payload.get("info") or {}
                # token_count should update the most recent emitted assistant
                # msg — flush buffer first so the totals attach to the right turn.
                _flush_assistant(buf, assistant_msgs)
                _attach_usage(assistant_msgs, info)
            else:
                progress_msgs.append({
                    "type": "progress",
                    "data": {"type": ptype or "event_msg"},
                    "content": payload,
                })

        elif rtype == "turn_context":
            # Metadata — ignored per spec.
            continue

        elif isinstance(payload, dict) and payload.get("role") == "user" and payload.get("type") == "message":
            # Defensive: bare user message record (no response_item wrapper).
            _flush_assistant(buf, assistant_msgs)
            msg = _convert_user_message_payload(payload)
            if msg:
                user_msgs.append(msg)

    _flush_assistant(buf, assistant_msgs)
    return assistant_msgs, user_msgs, progress_msgs, system_msgs, session_id, project_dir, default_model


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
    """Parse a Codex CLI session JSONL into the shape agent_dashcam_score consumes.

    Returned keys: provider, records, partial, assistant_msgs, user_msgs,
    progress_msgs, system_msgs, tool_names, tool_uses_with_input,
    assistant_text_lc, user_text_lc, session_id, project_dir, jsonl_lines,
    jsonl_bytes.
    """
    threshold = config.get("jsonl_tail_threshold_mb", 20) * 1024 * 1024
    tail_n = config.get("jsonl_tail_lines", 5000)
    records, partial = iter_records(path, threshold, tail_n)

    assistant_msgs, user_msgs, progress_msgs, system_msgs, session_id, project_dir, _model = _build_msg_streams(records)
    tool_names = extract_tool_uses(assistant_msgs)
    tool_uses_with_input = extract_tool_uses_with_input(assistant_msgs)

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
    """Yield CanonicalEvent from a Codex CLI session JSONL.

    Ordering follows the JSONL stream. Tokens from `token_count` event_msgs are
    attached to the most recent `agent_message` yielded by buffering ahead by
    one agent turn.
    """
    records, _ = iter_records(path, tail_threshold_bytes, tail_n)

    session_id = ""
    default_model: str | None = None
    # Look up session_id and default model from session_meta (first record
    # typically) so we can stamp every event.
    for rec in records:
        if rec.get("type") == "session_meta":
            payload = rec.get("payload") or {}
            if isinstance(payload, dict):
                session_id = payload.get("id") or session_id
                default_model = payload.get("model") or default_model
            break

    # Buffer for the current assistant turn so pending token_count can update it.
    pending_agent: dict | None = None

    def flush_pending() -> Iterator[CanonicalEvent]:
        nonlocal pending_agent
        if pending_agent is not None:
            yield CanonicalEvent(**pending_agent)
            pending_agent = None

    for rec in records:
        rtype = rec.get("type")
        ts = rec.get("timestamp") or ""
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}

        if rtype == "session_meta":
            yield CanonicalEvent(
                ts=ts, session_id=session_id, provider=PROVIDER,
                kind="system", role="system",
                text=json.dumps({
                    "id": payload.get("id"),
                    "cwd": payload.get("cwd"),
                    "model": payload.get("model"),
                }),
                raw=rec,
            )

        elif rtype == "response_item":
            ptype = payload.get("type")
            if ptype == "message" and payload.get("role") == "user":
                yield from flush_pending()
                text_parts: list[str] = []
                for c in payload.get("content") or []:
                    if isinstance(c, dict) and isinstance(c.get("text"), str):
                        text_parts.append(c["text"])
                if text_parts:
                    yield CanonicalEvent(
                        ts=ts, session_id=session_id, provider=PROVIDER,
                        kind="user_message", role="user",
                        text=" ".join(text_parts), raw=rec,
                    )

            elif ptype == "message" and payload.get("role") == "assistant":
                yield from flush_pending()
                text_parts = []
                for c in payload.get("content") or []:
                    if isinstance(c, dict) and isinstance(c.get("text"), str):
                        text_parts.append(c["text"])
                pending_agent = dict(
                    ts=ts, session_id=session_id, provider=PROVIDER,
                    kind="agent_message", role="agent",
                    text=" ".join(text_parts) if text_parts else None,
                    model=default_model,
                    raw=rec,
                )

            elif ptype == "reasoning":
                yield from flush_pending()
                text = _reasoning_text(payload)
                if text:
                    pending_agent = dict(
                        ts=ts, session_id=session_id, provider=PROVIDER,
                        kind="agent_message", role="agent",
                        text=text, model=default_model, raw=rec,
                    )

            elif ptype == "function_call":
                name = payload.get("name") or ""
                yield CanonicalEvent(
                    ts=ts, session_id=session_id, provider=PROVIDER,
                    kind="tool_call", role="agent",
                    tool_name=name,
                    tool_family=canonicalize_tool_name(name),
                    tool_input=_parse_arguments(payload.get("arguments")),
                    raw=rec,
                )

            elif ptype == "local_shell_call":
                yield CanonicalEvent(
                    ts=ts, session_id=session_id, provider=PROVIDER,
                    kind="tool_call", role="agent",
                    tool_name="local_shell_call",
                    tool_family=canonicalize_tool_name("local_shell_call"),
                    tool_input={"command": _local_shell_command(payload)},
                    raw=rec,
                )

            elif ptype == "web_search_call":
                inp: dict = {}
                q = payload.get("query")
                if isinstance(q, str):
                    inp["query"] = q
                yield CanonicalEvent(
                    ts=ts, session_id=session_id, provider=PROVIDER,
                    kind="tool_call", role="agent",
                    tool_name="web_search_call",
                    tool_family=canonicalize_tool_name("web_search_call"),
                    tool_input=inp,
                    raw=rec,
                )

        elif rtype == "function_call_output":
            yield from flush_pending()
            output = payload.get("output") or ""
            if not isinstance(output, str):
                try:
                    output = json.dumps(output)
                except (TypeError, ValueError):
                    output = str(output)
            yield CanonicalEvent(
                ts=ts, session_id=session_id, provider=PROVIDER,
                kind="tool_result", role="tool",
                text=output,
                tool_input={"tool_use_id": payload.get("call_id") or ""},
                raw=rec,
            )

        elif rtype == "event_msg":
            ptype = payload.get("type")
            if ptype == "token_count":
                info = payload.get("info") or {}
                # Per-turn marginal preferred over cumulative to avoid N*-overcount
                # when multiple token_count events fire across a session.
                totals = info.get("last_token_usage") if isinstance(info, dict) else None
                if not isinstance(totals, dict):
                    totals = info.get("total_token_usage") if isinstance(info, dict) else None
                if not isinstance(totals, dict):
                    totals = info if isinstance(info, dict) else {}
                if pending_agent is not None:
                    pending_agent["tokens_input"] = totals.get("input_tokens")
                    pending_agent["tokens_output"] = totals.get("output_tokens")
                    pending_agent["tokens_cache_read"] = totals.get("cached_input_tokens")
                    yield from flush_pending()
                else:
                    # Orphan token_count — emit a bare agent_message carrying tokens.
                    yield CanonicalEvent(
                        ts=ts, session_id=session_id, provider=PROVIDER,
                        kind="agent_message", role="agent",
                        tokens_input=totals.get("input_tokens"),
                        tokens_output=totals.get("output_tokens"),
                        tokens_cache_read=totals.get("cached_input_tokens"),
                        model=default_model,
                        raw=rec,
                    )
            else:
                yield CanonicalEvent(
                    ts=ts, session_id=session_id, provider=PROVIDER,
                    kind="progress",
                    text=str(ptype or "event_msg"),
                    raw=rec,
                )

    yield from flush_pending()
