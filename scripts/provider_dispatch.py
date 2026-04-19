"""Provider dispatch — pick the right adapter for a session JSONL.

Selection precedence:
  1. Explicit `provider` argument ("claude" | "codex" | "gemini").
  2. Auto-detect by path pattern (e.g. ~/.claude/projects → claude).
  3. Auto-detect by first-line JSON shape sniff.
  4. Fallback to claude (existing behaviour preserved).

Exposed:
  - SUPPORTED_PROVIDERS: tuple[str, ...]
  - detect_provider(path: Path) -> str
  - resolve_adapter(provider: str | None, path: Path) -> tuple[str, module]
"""
from __future__ import annotations

import json
from pathlib import Path

from adapters import claude as claude_adapter
from adapters import codex as codex_adapter
from adapters import gemini as gemini_adapter


SUPPORTED_PROVIDERS = ("claude", "codex", "gemini")
_ADAPTERS = {
    "claude": claude_adapter,
    "codex": codex_adapter,
    "gemini": gemini_adapter,
}


def _detect_by_path(path: Path) -> str | None:
    """Match the path against provider-native log directories."""
    parts = set(path.resolve().parts) if path.exists() else set(path.parts)
    if ".codex" in parts or ("codex" in parts and any("rollout" in p for p in path.parts)):
        return "codex"
    if ".gemini" in parts:
        return "gemini"
    if ".claude" in parts:
        return "claude"
    name = path.name
    if name.startswith("session-") and name.endswith(".json"):
        return "gemini"
    if name.startswith("rollout-") and name.endswith(".jsonl"):
        return "codex"
    return None


def _detect_by_first_line(path: Path) -> str | None:
    """Peek at the first non-empty JSON line and classify the envelope shape.

    Codex rollouts tag every record `{timestamp, type, payload}` and the first
    record is `type=session_meta`. Gemini sessions start with a conversation
    record `{role, parts, ...}`. Claude records have `type=user|assistant|...`
    with no top-level `parts` key.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    return None
                if not isinstance(rec, dict):
                    return None
                if rec.get("type") == "session_meta" and "payload" in rec:
                    return "codex"
                if "role" in rec and "parts" in rec:
                    return "gemini"
                if rec.get("type") in {"user", "assistant", "system", "summary"}:
                    return "claude"
                return None
    except OSError:
        return None
    return None


def detect_provider(path: Path) -> str:
    """Run path + first-line heuristics, fall back to 'claude'."""
    return (
        _detect_by_path(path)
        or _detect_by_first_line(path)
        or "claude"
    )


def resolve_adapter(provider: str | None, path: Path) -> tuple[str, object]:
    """Return (provider_name, adapter_module) for the given path.

    `provider` of None or "auto" triggers detection; unknown strings raise.
    """
    if provider is None or provider == "auto":
        provider = detect_provider(path)
    if provider not in _ADAPTERS:
        raise ValueError(f"unknown provider: {provider!r} (expected one of {SUPPORTED_PROVIDERS} or 'auto')")
    return provider, _ADAPTERS[provider]
