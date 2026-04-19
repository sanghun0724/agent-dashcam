# Changelog

All notable changes to Agent Dashcam are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — Multi-provider day-one close (Phases 1–3 + 5)

### Added

- **`docs/ARCHITECTURE.md`** — multi-provider design document: native-log survey (Claude / Codex / Gemini), canonical event schema, tool-family map, per-provider hook integration, 7-phase roadmap.
- **`scripts/canonical.py`** — `CanonicalEvent` dataclass (vendor-agnostic event wire format) + `TOOL_FAMILY_MAP` covering Claude PascalCase, Codex snake_case (`apply_patch`, `local_shell_call`, `web_search_call`), and Gemini names (`read_file`, `replace`, `run_shell_command`, `grep_search`, `save_memory`).
- **`scripts/adapters/claude.py`** — Claude Code JSONL parser exposing `load_session(path, config) -> dict` (scorer input) and `iter_events(path) -> Iterator[CanonicalEvent]` (OTel / observability input). Owns all Claude-specific JSONL shape knowledge.
- **`scripts/adapters/codex.py`** — Codex CLI rollout parser. Re-projects `{timestamp, type, payload}` envelopes (`session_meta`, `response_item`, `function_call_output`, `event_msg[token_count]`, `turn_context`) into the Claude-shaped msg dict the scorer consumes. Token totals from `event_msg.token_count` merge onto the preceding agent turn.
- **`scripts/adapters/gemini.py`** — Gemini CLI session JSON parser. Re-projects `{role, parts, metadata, usageMetadata}` into the Claude-shaped msg dict; `functionCall` → `tool_use`, `functionResponse` → `tool_result`, `usageMetadata` → Claude usage keys. `$rewindTo`/`$set` control markers tolerated (bucketed as `progress_msgs`).
- **`scripts/provider_dispatch.py`** + `agent_dashcam_score.py --provider {auto,claude,codex,gemini}` — CLI provider dispatch. `auto` (default) matches the path against provider-native directories (`~/.claude/projects`, `~/.codex/sessions`, `~/.gemini/tmp`), then falls back to a first-line JSON shape sniff (`type=session_meta` → codex, `role`+`parts` → gemini, `type=user|assistant|system|summary` → claude), then to a final `claude` default. Propagated through `bin/agent-dashcam score --provider ...`.
- **`hooks/codex-stop.mjs`** — Node ESM Stop-hook wrapper for Codex CLI. Resolves the rollout path from stdin (`transcript_path` / `rollout_path` direct; falls back to a newest-mtime scan of `~/.codex/sessions/**/rollout-*<session_id>*.jsonl`), then runs `agent_dashcam_score.py --provider codex --save`. `--dry-run` flag prints the resolved path without scoring.
- **`hooks/gemini-stop.mjs`** — Node ESM `SessionEnd`-hook wrapper for Gemini CLI. Accepts `transcript_path` (Gemini's native stdin field) or a `session_id` fallback that walks `~/.gemini/tmp/<project_hash>/chats/session-*.json`. Same `--dry-run` semantics.
- **Pricing tables** — `config.example.json` now includes OpenAI (`gpt-5-codex`, `gpt-5`, `o1-mini`, `o4-mini`) and Google (`gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-1.5-pro`, `gemini-1.5-flash`) entries with `input` / `output` / `cache_read` / `cache_write` rates in USD per 1M tokens, plus a `pricing_sources` block citing the vendor pricing pages and access date. Previously only Anthropic models were priced.
- **New test files** — `fixtures/test_adapter_claude.py` (15), `fixtures/test_adapter_codex.py` (19), `fixtures/test_adapter_gemini.py` (18), `fixtures/test_provider_dispatch.py` (18 — path + first-line heuristics, explicit and auto routing, CLI end-to-end), `fixtures/test_hook_wrappers.py` (8 — `node --check` + dry-run + end-to-end scoring through each wrapper), `fixtures/test_pricing_lookup.py` (8 — required-model coverage, rate lookup correctness, non-zero `cost_efficiency` for synthetic sessions of each new model family).
- **Weekly report** — `scripts/weekly_report.py` + `bin/agent-dashcam weekly` + `config.example.json` `weekly_report` block. Distinct weekly signals vs. the count-based daily view: time-windowed `[end - days, end)` load, week-over-week delta on `weighted_avg`, per-session combo-pattern frequency (counts how many sessions hit each of the 5 combos, vs. daily's single-point detect), golden-session rate (`% sessions ≥ 0.75`), 7-day activity sparkline (`" ▁▂▃▄▅▆▇█"`), and best/worst session picks. Shares axis-stats + suppression helpers with `daily_report.py`. 15 new tests in `fixtures/test_weekly_report.py`. Total: 137 tests.

### Changed

- **`scripts/agent_dashcam_score.py`** — parsing code (`tail_lines`, `iter_records`, `extract_tool_uses`, `extract_tool_uses_with_input`, `extract_assistant_text`, `extract_user_text`) moved into `adapters/claude.py`. `score_jsonl(path, config, provider=None)` now delegates to the provider dispatcher; the default path (provider omitted / `auto`) preserves the previous behaviour for Claude sessions. Codex + Gemini sessions flow through the same `score_jsonl()` with `--provider codex|gemini` (or auto-detected).

### Known limitations

- **Scorer tool-family awareness is still Claude-biased** — `compute_read_edit_ratio`, `count_useful_outputs`, the `_READ_LIKE_TOOLS` / `_EDIT_LIKE_TOOLS` sets feeding `classify_session_type`, and the debug-branch `c.get("Bash", 0)` check all still match raw Claude PascalCase tool names. Codex/Gemini sessions score end-to-end (cost, tokens, sentiment, reasoning-loop, hook-health, operational-bottleneck all work), but `read_edit_ratio` and `cost_per_useful_output` still fall back to neutral values and those sessions never classify as `feature` / `debug` / `bugfix`. Lifting the scorer off PascalCase strings onto canonical families is the next planned phase (see `docs/ARCHITECTURE.md` §8, Phase 4).
- **Gemini `$rewindTo` over-counting** — rewound turns are still reflected in token / tool-use totals because the adapter does not truncate preceding content. Documented in `scripts/adapters/gemini.py` module docstring.
- **Gemini `usageMetadata.promptTokenCount` semantics unverified** — passed through to Claude `input_tokens` as-is. If Gemini CLI reports a cumulative running total rather than per-turn marginal, `cost_efficiency` and `context_efficiency` will inflate for long Gemini sessions. Needs validation against real Gemini session files.
- **Gemini cache-write pricing is heuristic** — Google bills context caching via TTL-based storage rather than per-token writes, so `gemini-*.cache_write` is set to the input rate as a fallback. Real long-session Gemini costs may diverge; treat `cost_efficiency` as indicative, not exact, for cache-heavy Gemini runs.

## [3.0.0] — 2026-04-19

Initial open-source release. Version tag aligns with `config.version: 3` in the scorer.

### Added

- **10-axis deterministic scoring** (`scripts/agent_dashcam_score.py`, Python stdlib only):
  - Context / cost: `context_efficiency`, `cost_efficiency`, `cost_per_useful_output`, `role_focus`
  - Interaction quality (lucemia empirical): `read_edit_ratio`, `reasoning_loop`, `sentiment`
  - Infrastructure health: `constraint_adherence`, `hook_health`, `operational_bottleneck`
- **Session auto-classification** — 8 types (`feature` / `bugfix` / `refactor` / `explore` / `debug` / `docs` / `meta` / `mixed`) with per-type axis suppression table. Prevents false-positive action items for sessions whose low axes are natural (e.g. zero commits during a refactor).
- **Env-Up pipeline** (`scripts/envup.py`) — detects Claude Code version bumps, fetches release notes, diffs against a 46-entry `known-issues.json`, flags workarounds that can now be deleted.
- **3-hook pattern** (`hooks/`):
  - `session-start.mjs` — briefs the next conversation with weighted_avg + lowest non-suppressed axes + one actionable tip
  - `session-stop.mjs` — triggers the scorer out-of-band
- **Dynamic threshold auto-calibration** (`scripts/retention.py`) — p20/p80 of recent session distribution; only kicks in after 30 samples.
- **Daily report + Slack payload** (`scripts/daily_report.py`) — markdown report + Slack Blocks JSON with weak-axis action items, session-type distribution, suppression notes, and combo patterns.
- **Combo detection** — 5 patterns (Opus overuse / analysis paralysis / flailing / environment rot / golden session).
- **Grafana dashboard** (`grafana/dashboard.json`) — time-series for all 10 axes.
- **Prometheus exporter** (`scripts/export_prometheus.py`).
- **Agent Dashcam CLI** (`bin/agent-dashcam`) — `score` / `daily` / `envup` / `calibrate` / `status`.
- **`AGENT_DASHCAM_ROOT` env var** — all Python scripts + Node hooks read `$AGENT_DASHCAM_ROOT` before falling back to `~/.claude/agent-dashcam/`. `load_config()` falls back to `config.example.json` when `config.json` is missing, so tests and fresh clones run without bootstrap.
- **36 unit tests** covering all axes, classifier, calibration, schema drift, and integration on fixture JSONLs.

### Design

- Measurement and judgement split: deterministic Python scorer, no LLM self-grading.
- Zero tokens on the hot path: all scoring runs out-of-band in a hook, briefing happens on *next* session start.
- Stdlib-only Python + Node ESM — no external deps.
