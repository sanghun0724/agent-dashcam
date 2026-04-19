# Agent Dashcam Architecture — Multi-Provider Extension

Agent Dashcam began as a Claude-Code-only scorer. The 10 axes, however, are not Claude-specific —
they measure properties of *any* coding agent's session (tool-call mix, token-to-output
efficiency, retry density, etc). This document describes how Agent Dashcam is being restructured
so Codex CLI and Gemini CLI sessions can be scored with the same core logic.

---

## 1. Provider-native session logs (survey)

| Provider | Log path | Format | Tool-name style | Hooks |
|---|---|---|---|---|
| Claude Code | `~/.claude/projects/<project>/<session>.jsonl` | JSONL | PascalCase (`Read`, `Edit`, `Bash`) | `SessionStart`, `SessionStop` (shell command in settings) |
| Codex CLI | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (+ SQLite index) | JSONL, tagged `{type, payload}` | snake_case (`function_call.name = read_file`, `local_shell_call`, `web_search_call`) | `~/.codex/hooks.json`: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop` |
| Gemini CLI | `~/.gemini/tmp/<project_hash>/chats/session-<uuid>.json` (JSONL despite extension) | JSONL, rewind-aware (`{$rewindTo}`, `{$set}`) | snake_case (`read_file`, `run_shell_command`, `replace`, `grep_search`) | `settings.json` → `hooks`: `SessionStart`, `SessionEnd`, `BeforeTool`, `AfterTool`, … |

All three expose enough information — per-turn token usage, tool-call inventory, user/assistant
text — to compute every Agent Dashcam axis. Differences are syntactic (field names, casing,
envelope shape), not semantic.

---

## 2. Design goals

1. **Keep the 10 axes unchanged** — if the measurement is good for Claude, it is good for Codex/Gemini.
2. **No per-provider branches in the scorer** — one code path, fed by adapter output.
3. **Adapters absorb all vendor-specific knowledge** — JSONL layout, field names, tool aliases, cost tables.
4. **Canonical event stream is the wire format** — a Agent Dashcam-native JSONL schema that any adapter emits and the scorer consumes. Future observability tools (OTel exporter, Grafana, etc.) subscribe to the same canonical stream.
5. **Backwards-compatible rollout** — Claude users see no behavior change. Phase 1 introduces the canonical layer without changing score outputs.

---

## 3. Layered architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ native logs                                                          │
│  ~/.claude/projects/*/*.jsonl                                        │
│  ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl                        │
│  ~/.gemini/tmp/*/chats/session-*.json                                │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
          ┌─────────────────────┴─────────────────────┐
          │      scripts/adapters/                    │
          │        claude.py      (Phase 1)           │
          │        codex.py       (Phase 2)           │
          │        gemini.py      (Phase 3)           │
          │                                           │
          │      each module exposes:                 │
          │        load_session(path, cfg) -> dict    │
          │        iter_events(path) -> Iter[Event]   │
          └─────────────────────┬─────────────────────┘
                                │
                                ▼
          ┌───────────────────────────────────────────┐
          │    scripts/canonical.py                   │
          │      CanonicalEvent dataclass             │
          │      TOOL_FAMILY_MAP (dict)               │
          │      canonicalize_tool_name()             │
          │      READ_LIKE_FAMILIES / EDIT_LIKE_…     │
          └─────────────────────┬─────────────────────┘
                                │
                                ▼
          ┌───────────────────────────────────────────┐
          │    scripts/agent_dashcam_score.py               │
          │      compute_*  (10 axes)                 │
          │      classify_session_type                │
          │      score_jsonl (oriented around         │
          │                    adapter output)        │
          └───────────────────────────────────────────┘
```

---

## 4. Canonical event schema

Defined in `scripts/canonical.py`. Every adapter yields a stream of these.

```python
@dataclass(frozen=True)
class CanonicalEvent:
    ts: str                            # ISO-8601 UTC
    session_id: str                    # vendor session ID
    provider: str                      # "claude" | "codex" | "gemini"
    kind: str                          # "user_message" | "agent_message"
                                       # | "tool_call"    | "tool_result"
                                       # | "system"       | "progress"
    role: str | None                   # "user" | "agent" | "tool" | "system"
    text: str | None                   # flattened text payload
    tool_name: str | None              # raw vendor tool name
    tool_family: str | None            # canonical family (see §5)
    tool_input: dict | None            # vendor-specific args (preserved)
    tokens_input: int | None
    tokens_output: int | None
    tokens_cache_read: int | None
    tokens_cache_write: int | None
    model: str | None
    raw: dict | None                   # original record (for auditing)
```

### `kind` taxonomy

- `user_message` — human prompt.
- `agent_message` — model-produced text (reasoning + final answer).
- `tool_call` — model invoked a tool (args in `tool_input`).
- `tool_result` — tool output (pair with the matching `tool_call` by id in `raw`).
- `system` — system prompt, notice, or CLI-internal message.
- `progress` — hook/status events (bounce, retry, background task, etc).

---

## 5. Tool family canonicalization

A single map normalizes vendor tool names to a short family vocabulary the scorer uses
(`read_edit_ratio`, `role_focus`, etc). Unknown tools fall back to family `other`.

| Canonical family | Claude | Codex | Gemini |
|---|---|---|---|
| `read` | `Read` | `function_call(name=read_file)` | `read_file` |
| `edit` | `Edit`, `MultiEdit`, `NotebookEdit` | `function_call(name=apply_patch)` | `replace` |
| `write` | `Write` | `function_call(name=write_file)` | `write_file` |
| `glob` | `Glob` | `function_call(name=glob)` | `glob` |
| `grep` | `Grep` | `function_call(name=grep)` | `grep_search` |
| `bash` | `Bash` | `local_shell_call` | `run_shell_command` |
| `web_fetch` | `WebFetch` | `function_call(name=web_fetch)` | `web_fetch` |
| `web_search` | `WebSearch` | `web_search_call` | `web_search` |
| `todo` | `TodoWrite` | — | `save_memory` (approximate) |
| `task` | `Task` | — | — |
| `other` | fallback | fallback | fallback |

`NotebookEdit` is normalised into the `edit` family (no separate `notebook` family).

The `read_edit_ratio` axis compares the `read` family count to the `edit ∪ write` count
regardless of provider — same semantic, different vendor wire format.

---

## 6. Hook integration (per provider)

Each provider has a distinct hook manifest. The scoring hook is always a thin shell/Node
wrapper that (a) identifies the session's native log path and (b) runs the appropriate
adapter + `agent_dashcam_score.py`.

### Claude Code (`~/.claude/settings.json`)
```json
{
  "hooks": {
    "SessionStop": [{ "hooks": [{ "type": "command",
      "command": "node $HOME/.claude/agent-dashcam/hooks/session-stop.mjs" }]}],
    "SessionStart": [{ "hooks": [{ "type": "command",
      "command": "node $HOME/.claude/agent-dashcam/hooks/session-start.mjs" }]}]
  }
}
```
Current implementation — no change in Phase 1.

### Codex CLI (`~/.codex/hooks.json`, Phase 2)
```json
{
  "hooks": {
    "Stop": [{ "command": "node ~/.claude/agent-dashcam/hooks/codex-stop.mjs" }],
    "SessionStart": [{ "command": "node ~/.claude/agent-dashcam/hooks/codex-start.mjs" }]
  }
}
```
Codex hook stdin provides `session_id` + rollout path — the adapter resolves the JSONL from there.

### Gemini CLI (`~/.gemini/settings.json`, Phase 3)
```json
{
  "hooks": {
    "SessionEnd": [{ "type": "command",
      "command": "node ~/.claude/agent-dashcam/hooks/gemini-stop.mjs" }]
  }
}
```
Gemini hook stdin includes `transcript_path` — direct pointer to the session JSONL.

---

## 7. Data flow during a scoring run

```
native JSONL ──► adapter.load_session(path, config) ──► session_data (dict)
                                                            │
                                                            ▼
                                                 classify_session_type
                                                            │
                                                            ▼
                                                 compute_<each_axis>
                                                            │
                                                            ▼
                                                  scores/<project>__<session>.json
```

`session_data` in Phase 1 is a dict with the Claude-shaped fields the current scorer
already consumes (`user_msgs`, `assistant_msgs`, `tool_names`, `total_usd`,
`total_output`, `assistant_text`, `user_text`, …). In Phase 2+, Codex/Gemini adapters
produce the same dict shape by re-projecting their own events — the scorer stays
provider-agnostic.

---

## 8. Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Adapter + canonical scaffolding; Claude adapter wraps existing parser | **shipped** |
| 2 | Codex adapter (parser + tests; hooks + OpenAI pricing table tracked as follow-up) | **shipped** |
| 3 | Gemini adapter (parser + tests; hooks + Gemini pricing table tracked as follow-up) | **shipped** |
| 4 | Scorer tool-family awareness — lift `compute_read_edit_ratio`, `count_useful_outputs`, and `classify_session_type` off Claude PascalCase strings onto canonical families so Codex/Gemini sessions get full fidelity on all 10 axes | planned |
| 5 | Hook wrappers (`hooks/codex-stop.mjs`, `hooks/gemini-stop.mjs`) + CLI provider dispatch + OpenAI / Gemini pricing tables | **shipped** |
| 6 | OTel GenAI exporter (canonical event → OTLP) | planned |
| 7 | Per-provider suppression rules (e.g., Codex `reasoning_tokens` adjusts `reasoning_loop` baseline) | planned |

---

## 9. Non-goals

- **Not** a unified billing ledger. Each provider has its own cost table; Agent Dashcam
  records USD per session but does not reconcile invoices.
- **Not** an agent orchestrator. Agent Dashcam observes; it does not route prompts between providers.
- **Not** a replacement for OTel instrumentation on the CLI side. Where providers
  offer first-class OTel (Codex, Gemini), Agent Dashcam complements it with axis-level
  scoring; it does not re-export raw spans.
