"""Agent Dashcam canonical event schema — vendor-agnostic session representation.

Adapters (scripts/adapters/*.py) yield CanonicalEvent streams; the scorer and any
downstream observability tools consume only this schema. No provider-specific field
names leak past an adapter.

See docs/ARCHITECTURE.md for the larger picture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Provider = Literal["claude", "codex", "gemini"]
EventKind = Literal[
    "user_message",
    "agent_message",
    "tool_call",
    "tool_result",
    "system",
    "progress",
]
Role = Literal["user", "agent", "tool", "system"]


TOOL_FAMILY_MAP: dict[str, str] = {
    # read
    "Read": "read",
    "read_file": "read",
    # edit
    "Edit": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "apply_patch": "edit",
    "replace": "edit",
    # write
    "Write": "write",
    "write_file": "write",
    # glob
    "Glob": "glob",
    "glob": "glob",
    # grep
    "Grep": "grep",
    "grep_search": "grep",
    # bash / shell
    "Bash": "bash",
    "run_shell_command": "bash",
    "local_shell_call": "bash",
    # web
    "WebFetch": "web_fetch",
    "web_fetch": "web_fetch",
    "WebSearch": "web_search",
    "web_search": "web_search",
    "web_search_call": "web_search",
    # todo / planning
    "TodoWrite": "todo",
    "save_memory": "todo",
    # task / subagent
    "Task": "task",
    "tool_search_call": "task",
}

EDIT_LIKE_FAMILIES: frozenset[str] = frozenset({"edit", "write"})
READ_LIKE_FAMILIES: frozenset[str] = frozenset({"read"})


def canonicalize_tool_name(name: str | None) -> str:
    """Map a vendor tool name to a canonical family, or 'other'."""
    if not name:
        return "other"
    return TOOL_FAMILY_MAP.get(name, "other")


@dataclass(frozen=True)
class CanonicalEvent:
    """One normalized event in a Agent Dashcam session stream.

    A session is a sequence of CanonicalEvent objects ordered by `ts`. Fields
    irrelevant to a specific event kind are `None` (e.g. `tokens_output` on a
    `tool_call` event).
    """

    ts: str
    session_id: str
    provider: Provider
    kind: EventKind
    role: Role | None = None
    text: str | None = None
    tool_name: str | None = None
    tool_family: str | None = None
    tool_input: dict | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    tokens_cache_read: int | None = None
    tokens_cache_write: int | None = None
    model: str | None = None
    raw: dict | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        out: dict = {
            "ts": self.ts,
            "session_id": self.session_id,
            "provider": self.provider,
            "kind": self.kind,
        }
        for key in (
            "role",
            "text",
            "tool_name",
            "tool_family",
            "tool_input",
            "tokens_input",
            "tokens_output",
            "tokens_cache_read",
            "tokens_cache_write",
            "model",
        ):
            val = getattr(self, key)
            if val is not None:
                out[key] = val
        return out
