#!/usr/bin/env python3
"""Agent Dashcam — 10축 deterministic 채점 스크립트 (v3).

입력: JSONL 파일 경로 (Claude Code 세션 로그)
출력: stdout에 10축 점수 JSON. --save 플래그 사용 시 ~/.claude/agent-dashcam/scores/ 에도 저장.

10축 (모두 0~1, higher=better):
  [v1 6축]
  - context_efficiency: cache_read 비율 (= Anthropic 공식 cache hit ratio)
  - cost_efficiency: output tokens/$ 로그 정규화 (임계값 config.cost_efficiency_thresholds)
  - role_focus: tool 사용 분포의 집중도 (1 - normalized Shannon entropy)
  - constraint_adherence: 1 - (tool_result is_error + api_error) / total_events
  - hook_health: 1 - (hook 관련 에러 / hook 총 실행)
  - operational_bottleneck: 1 - (compact_boundary 발생률 + turn_duration penalty)

  [v2 추가 — lucemia]
  - read_edit_ratio: Read:Edit 비율 (lucemia, 234k+ tool call 분석 기반)

  [v3 추가]
  - cost_per_useful_output: $ / (commits + PRs + tests passed) — DX Core 4 준거
  - reasoning_loop: 자기재시도 언어 빈도 (lucemia, <10/1K calls good, >20 degraded)
  - sentiment: user 메시지 positive:negative 비율 (>=4:1 good, <3:1 degraded)

20MB 초과 JSONL은 마지막 N줄만 tail (config.jsonl_tail_lines). meta.partial_score=True 마킹.
필수 필드 누락 시 meta.schema_drift 리스트에 기록하되 crash하지 않음.

임계값 출처:
- cost_efficiency 500/50k: 휴리스틱 (업계 표준 없음, retention.py 월말 자동 튜닝)
- read_edit_ratio 2/6: lucemia/claude-session-analyzer 실증 (234k+ tool calls)
- cost_per_useful_output 10/0.1 USD: DX Core 4 framework (Utilization+Impact+Cost)
- reasoning_loop 10/20 per 1K calls: lucemia 권장값
- sentiment 3:1/4:1 pos:neg: lucemia 권장값
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from adapters.claude import (  # noqa: E402
    load_session as _claude_load_session,
    safe_get,
)
from provider_dispatch import resolve_adapter  # noqa: E402


def load_session(path, config, provider=None):
    """Dispatch to the correct provider adapter's load_session.

    `provider` of None or "auto" triggers auto-detect. Defaults preserve the
    pre-dispatch behaviour (claude) for unknown shapes.
    """
    _name, adapter = resolve_adapter(provider, path)
    return adapter.load_session(path, config)


AGENT_DASHCAM_ROOT = Path(os.environ.get("AGENT_DASHCAM_ROOT") or (Path.home() / ".claude" / "agent-dashcam"))
CONFIG_PATH = AGENT_DASHCAM_ROOT / "config.json"


def load_config() -> dict:
    path = CONFIG_PATH if CONFIG_PATH.exists() else AGENT_DASHCAM_ROOT / "config.example.json"
    with open(path) as f:
        return json.load(f)


def compute_context_efficiency(assistant_msgs: list[dict]) -> float:
    total_cache_read = 0
    total_other = 0
    for msg in assistant_msgs:
        usage = safe_get(msg, "message", "usage", default={}) or {}
        cr = usage.get("cache_read_input_tokens", 0) or 0
        cc = usage.get("cache_creation_input_tokens", 0) or 0
        inp = usage.get("input_tokens", 0) or 0
        total_cache_read += cr
        total_other += cc + inp
    denom = total_cache_read + total_other
    if denom == 0:
        return 0.5
    return total_cache_read / denom


def compute_cost(assistant_msgs: list[dict], model_rates: dict) -> tuple[float, int]:
    total_usd = 0.0
    total_output = 0
    default_rate = model_rates.get("default", {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75})
    for msg in assistant_msgs:
        m = safe_get(msg, "message", default={}) or {}
        model = m.get("model", "default")
        rates = model_rates.get(model, default_rate)
        usage = m.get("usage", {}) or {}
        inp = usage.get("input_tokens", 0) or 0
        out = usage.get("output_tokens", 0) or 0
        cr = usage.get("cache_read_input_tokens", 0) or 0
        cc = usage.get("cache_creation_input_tokens", 0) or 0
        total_usd += (
            inp * rates.get("input", 3.0)
            + out * rates.get("output", 15.0)
            + cr * rates.get("cache_read", 0.3)
            + cc * rates.get("cache_write", 3.75)
        ) / 1_000_000
        total_output += out
    return total_usd, total_output


def compute_cost_efficiency(total_usd: float, total_output: int, lo: float = 500.0, hi: float = 50_000.0) -> float:
    """Score = output tokens per dollar, normalized by log scale.

    lo/hi는 config.cost_efficiency_thresholds 에서 주입. 기본값 500/50k는 휴리스틱.
    Anthropic 공식 eval 가이드도 수치 임계값 미제시 — 본인 분포로 튜닝 권장.
    """
    if total_usd <= 0:
        return 0.5 if total_output == 0 else 1.0
    out_per_dollar = total_output / total_usd
    if out_per_dollar <= lo:
        return 0.0
    if out_per_dollar >= hi:
        return 1.0
    return (math.log(out_per_dollar) - math.log(lo)) / (math.log(hi) - math.log(lo))


def compute_read_edit_ratio(tool_names: list[str], degraded: float = 2.0, good: float = 6.0) -> tuple[float, float]:
    """Read:Edit 비율 기반 사고 깊이 근사.

    lucemia/claude-session-analyzer 연구 (234k tool calls 분석):
      - ratio >= 6.0: good (깊은 사고)
      - 2.0 ~ 6.0: transition
      - ratio <= 2.0: degraded (쥐어짜는 코딩)

    Edit 계열 = Edit, Write, MultiEdit, NotebookEdit.
    Edit 0건이면 탐색/리서치 세션으로 간주, 0.5 반환.

    Returns (score, raw_ratio).
    """
    read_count = sum(1 for n in tool_names if n == "Read")
    edit_count = sum(1 for n in tool_names if n in ("Edit", "Write", "MultiEdit", "NotebookEdit"))
    if edit_count == 0:
        return (0.5, float("inf") if read_count > 0 else 0.0)
    ratio = read_count / edit_count
    if ratio <= degraded:
        return (0.0, ratio)
    if ratio >= good:
        return (1.0, ratio)
    return ((ratio - degraded) / (good - degraded), ratio)


_USEFUL_BASH_PATTERNS = (
    re.compile(r"\bgit\s+commit\b"),
    re.compile(r"\bgh\s+pr\s+create\b"),
    re.compile(r"\bgh\s+pr\s+merge\b"),
    re.compile(r"\b(npm|yarn|pnpm)\s+(run\s+)?(test|t)\b"),
    re.compile(r"\bpy(thon3?)?\s+-m\s+(pytest|unittest)\b"),
    re.compile(r"\bpytest\b"),
    re.compile(r"\bmake\s+test\b"),
    re.compile(r"\bswift\s+test\b"),
    re.compile(r"\bcargo\s+test\b"),
    re.compile(r"\bgo\s+test\b"),
    re.compile(r"\bbundle\s+exec\s+(rspec|rails\s+test)\b"),
    re.compile(r"\btuist\s+test\b"),
)


def count_useful_outputs(tool_uses: list[tuple[str, dict]]) -> dict:
    """Bash 커맨드에서 commit / PR / test 실행 횟수 카운트."""
    commits = 0
    prs = 0
    tests = 0
    for name, inp in tool_uses:
        if name != "Bash":
            continue
        cmd = str(inp.get("command") or "").lower()
        if not cmd:
            continue
        if re.search(r"\bgit\s+commit\b", cmd):
            commits += 1
        if re.search(r"\bgh\s+pr\s+create\b", cmd) or re.search(r"\bgh\s+pr\s+merge\b", cmd):
            prs += 1
        test_hit = False
        for pat in _USEFUL_BASH_PATTERNS[3:]:  # skip commit/PR patterns
            if pat.search(cmd):
                test_hit = True
                break
        if test_hit:
            tests += 1
    return {"commits": commits, "prs": prs, "tests": tests, "total": commits + prs + tests}


def compute_cost_per_useful_output(total_usd: float, useful_count: int, lo: float = 10.0, hi: float = 0.10) -> float:
    """$ / useful output 역로그 정규화.

    DX Core 4 framework: Utilization + Impact + Cost.
    useful output = commits + PR creation/merge + test 실행 횟수 (Bash 커맨드 파싱).

    lo >= hi 이므로 log 정규화 반대 방향:
      - cost_per >= lo (10 USD/output = 비싸다): 0.0
      - cost_per <= hi (0.10 USD/output = 저렴): 1.0
      - useful_count == 0: 0.5 (판단 불가)

    임계값 10/0.1 USD: 휴리스틱, retention.py가 월말 p20/p80로 자동 튜닝.
    """
    if useful_count == 0:
        return 0.5
    if total_usd <= 0:
        return 1.0
    cost_per = total_usd / useful_count
    if cost_per >= lo:
        return 0.0
    if cost_per <= hi:
        return 1.0
    # log scale: higher cost_per → lower score
    return (math.log(lo) - math.log(cost_per)) / (math.log(lo) - math.log(hi))


_REASONING_LOOP_PATTERNS = (
    "simplest fix",
    "let me try again",
    "let me try a different",
    "actually, let me",
    "actually let me",
    "that didn't work",
    "that did not work",
    "my mistake",
    "sorry, let me",
    "sorry let me",
    "let me retry",
    "wait, let me",
    "wait let me",
    "hmm, let me",
    "on second thought",
)


def compute_reasoning_loop(assistant_text_lc: str, total_tool_calls: int, degraded_per_1k: float = 20.0, good_per_1k: float = 10.0) -> tuple[float, int, float]:
    """자기재시도 언어 빈도 기반 reasoning loop 측정 (lucemia 방법론).

    assistant 텍스트에서 "simplest fix", "let me try again" 등 재시도 시그널 카운트.

    per_1k 기준 (tool call 1000건당):
      - <= good (10/1K): 1.0 (깔끔한 진행)
      - >= degraded (20/1K): 0.0 (재시도 지옥)
      - 구간: 선형

    Returns (score, raw_count, per_1k).
    """
    if not assistant_text_lc:
        return (0.5, 0, 0.0)
    count = sum(assistant_text_lc.count(p) for p in _REASONING_LOOP_PATTERNS)
    if total_tool_calls <= 0:
        return (0.5, count, 0.0)
    per_1k = (count / total_tool_calls) * 1000.0
    if per_1k >= degraded_per_1k:
        return (0.0, count, per_1k)
    if per_1k <= good_per_1k:
        return (1.0, count, per_1k)
    # linear interp
    score = 1.0 - (per_1k - good_per_1k) / (degraded_per_1k - good_per_1k)
    return (max(0.0, min(1.0, score)), count, per_1k)


_POS_KEYWORDS = (
    "thanks", "thank you", "great", "perfect", "awesome", "nice", "good job",
    "works", "working", "exactly", "correct", "brilliant", "love it",
    "좋아", "좋네", "완벽", "고마워", "감사", "잘됐", "잘 됐", "맞아",
    "멋지", "훌륭", "대박", "굿", "ㄱㄱ", "굳", "잘했",
)

_NEG_KEYWORDS = (
    "wrong", "broken", "failed", "fail ", "doesn't work", "does not work",
    "bad", "terrible", "stop ", "don't ", "do not ", "why did you",
    "that's wrong", "nope", "no,", "no.", "incorrect",
    "아니야", "아니", "틀렸", "이상해", "실패", "안 돼", "안돼", "안됨",
    "하지마", "멈춰", "그만", "왜 그렇게", "망했", "이상",
)


def compute_sentiment(user_text_lc: str) -> tuple[float, dict]:
    """User 메시지 positive:negative 키워드 비율 (lucemia).

    임계값 (pos:neg ratio):
      - >= 4: 1.0 (긍정 대화)
      - <= 3: 0.0 (부정 대화, 실제로는 3 미만으로 처리)
      - 구간 3~4: 선형
      - 양쪽 0: 0.5 (중립/단조 요청)
    """
    if not user_text_lc:
        return (0.5, {"pos": 0, "neg": 0, "ratio": None})
    pos = sum(user_text_lc.count(k) for k in _POS_KEYWORDS)
    neg = sum(user_text_lc.count(k) for k in _NEG_KEYWORDS)
    if pos == 0 and neg == 0:
        return (0.5, {"pos": 0, "neg": 0, "ratio": None})
    if neg == 0:
        return (1.0, {"pos": pos, "neg": 0, "ratio": float("inf")})
    ratio = pos / neg
    if ratio >= 4.0:
        return (1.0, {"pos": pos, "neg": neg, "ratio": round(ratio, 3)})
    if ratio <= 3.0:
        # 3 이하는 단조 감소: ratio 0 → 0.0, 3 → 0.0 (경계), 실제로는 3 미만도 0으로 처리
        score = max(0.0, ratio / 3.0 * 0.0)  # keep 0 below 3
        return (score, {"pos": pos, "neg": neg, "ratio": round(ratio, 3)})
    # 3~4 구간 선형
    score = (ratio - 3.0) / (4.0 - 3.0)
    return (max(0.0, min(1.0, score)), {"pos": pos, "neg": neg, "ratio": round(ratio, 3)})


def compute_role_focus(tool_names: list[str]) -> float:
    """Shannon entropy of tool distribution, normalized to [0, 1].
    Returns 1 - normalized_entropy so focused sessions score higher.
    """
    if not tool_names:
        return 0.5
    unique = len(set(tool_names))
    if unique <= 1:
        return 1.0
    counts = Counter(tool_names)
    total = sum(counts.values())
    entropy = -sum((c / total) * math.log2(c / total) for c in counts.values())
    max_entropy = math.log2(unique)
    if max_entropy == 0:
        return 1.0
    normalized = entropy / max_entropy
    return max(0.0, min(1.0, 1.0 - normalized))


def compute_constraint_adherence(user_msgs: list[dict], system_msgs: list[dict], total_tool_calls: int) -> float:
    errors = 0
    for msg in user_msgs:
        content = safe_get(msg, "message", "content", default=[]) or []
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                errors += 1
    for msg in system_msgs:
        if msg.get("subtype") == "api_error":
            errors += 1
    if total_tool_calls == 0:
        return 1.0 if errors == 0 else 0.0
    error_rate = errors / total_tool_calls
    return max(0.0, 1.0 - error_rate)


def compute_hook_health(progress_msgs: list[dict], system_msgs: list[dict]) -> float:
    total_hooks = sum(1 for m in progress_msgs if safe_get(m, "data", "type") == "hook_progress")
    if total_hooks == 0:
        return 1.0
    hook_errors = 0
    for m in system_msgs:
        content = (m.get("content") or "").lower()
        if "hook" in content and ("error" in content or "fail" in content or "timeout" in content):
            hook_errors += 1
        if m.get("level") == "error" and "hook" in content:
            hook_errors += 1
    error_rate = hook_errors / total_hooks
    return max(0.0, 1.0 - error_rate)


def compute_operational_bottleneck(system_msgs: list[dict], assistant_msgs: list[dict]) -> float:
    compact_count = sum(1 for m in system_msgs if m.get("subtype") == "compact_boundary")
    turn_durations: list[float] = []
    for m in system_msgs:
        if m.get("subtype") == "turn_duration":
            try:
                content = m.get("content", "")
                if isinstance(content, (int, float)):
                    turn_durations.append(float(content))
                elif isinstance(content, str):
                    parts = "".join(c for c in content if c.isdigit() or c == ".")
                    if parts:
                        turn_durations.append(float(parts))
            except (ValueError, TypeError):
                continue
    total_turns = len(assistant_msgs)
    compact_penalty = 0.0
    if total_turns > 0:
        compact_penalty = min(1.0, compact_count / max(total_turns / 10, 1))
    duration_penalty = 0.0
    if turn_durations:
        p95 = statistics.quantiles(turn_durations, n=20)[18] if len(turn_durations) >= 20 else max(turn_durations)
        duration_penalty = min(1.0, p95 / 120000.0)
    penalty = min(1.0, compact_penalty * 0.6 + duration_penalty * 0.4)
    return max(0.0, 1.0 - penalty)


_SESSION_TYPES = (
    "feature", "bugfix", "refactor", "explore", "debug", "docs", "meta", "mixed",
)

# 타입별 자연히 낮을 수 있는 축 — 리포트/알림에서 무시 처리.
SESSION_TYPE_SUPPRESSIONS: dict[str, tuple[str, ...]] = {
    "refactor": ("cost_per_useful_output", "read_edit_ratio"),
    "explore":  ("cost_per_useful_output", "read_edit_ratio"),
    "debug":    ("sentiment",),
    "docs":     ("cost_per_useful_output", "role_focus"),
    "meta":     ("cost_per_useful_output", "role_focus"),
    "feature":  (),
    "bugfix":   (),
    "mixed":    (),
}

_READ_LIKE_TOOLS = {"Read", "Grep", "Glob", "Agent", "Task", "WebFetch", "WebSearch"}
_EDIT_LIKE_TOOLS = {"Edit", "Write", "NotebookEdit"}
_DEBUG_KEYWORDS = re.compile(
    r"\b(error|exception|stack ?trace|traceback|failed|failure|bug|regression|"
    r"crash|panic|broken|wrong|틀렸|깨졌|오류|에러)\b",
    re.IGNORECASE,
)


def _count_md_only_edits(tool_uses_with_input: list[tuple[str, dict]]) -> tuple[int, int]:
    """Return (md_edit_count, total_edit_count) to decide docs sessions."""
    md = 0
    total = 0
    for name, inp in tool_uses_with_input:
        if name not in _EDIT_LIKE_TOOLS:
            continue
        total += 1
        fp = str(inp.get("file_path") or inp.get("notebook_path") or "").lower()
        if fp.endswith(".md") or fp.endswith(".mdx") or fp.endswith(".markdown"):
            md += 1
    return md, total


def classify_session_type(
    tool_names: list[str],
    tool_uses_with_input: list[tuple[str, dict]],
    useful_counts: dict,
    assistant_text_lc: str,
) -> tuple[str, float]:
    """Tool 분포 + useful output + assistant text 로 세션 타입 추정.

    반환값: (type, confidence 0~1). 데이터 부족 시 ('mixed', 0).
    우선순위는 첫 매칭 기준 — feature > docs > explore > refactor > debug > bugfix > mixed.
    """
    total = len(tool_names)
    if total == 0:
        return "mixed", 0.0

    c = Counter(tool_names)
    read_like = sum(c.get(t, 0) for t in _READ_LIKE_TOOLS)
    edit_like = sum(c.get(t, 0) for t in _EDIT_LIKE_TOOLS)
    commits = useful_counts.get("commits", 0)
    prs = useful_counts.get("prs", 0)
    tests = useful_counts.get("tests", 0)
    md_edits, total_edits = _count_md_only_edits(tool_uses_with_input)
    has_debug_kw = bool(_DEBUG_KEYWORDS.search(assistant_text_lc or ""))

    read_ratio = read_like / total
    edit_ratio = edit_like / total

    # 1) feature: 커밋 or PR 가 있으면 거의 확실
    if commits >= 1 or prs >= 1:
        conf = min(1.0, 0.6 + 0.1 * (commits + prs) + 0.05 * min(tests, 3))
        return "feature", round(conf, 3)

    # 2) docs: Edit 이 존재하고 80%+ 가 .md
    if total_edits >= 2 and md_edits / max(total_edits, 1) >= 0.8:
        return "docs", round(0.6 + 0.3 * (md_edits / total_edits), 3)

    # 3) explore: Read 계열이 60% 이상 + Edit 2건 이하
    if read_ratio >= 0.6 and edit_like <= 2:
        return "explore", round(min(1.0, 0.5 + read_ratio * 0.5), 3)

    # 4) debug: debug 키워드 + Bash + 일부 Edit
    if has_debug_kw and c.get("Bash", 0) >= 3 and tests >= 1:
        return "debug", 0.7

    # 5) meta: Edit 이 설정/스크립트 파일에 집중 (refactor 보다 먼저 체크)
    # 간단 휴리스틱 — 편집 타겟이 config/settings 이름 포함 비율 ≥ 0.6
    cfg_hits = 0
    for name, inp in tool_uses_with_input:
        if name not in _EDIT_LIKE_TOOLS:
            continue
        fp = str(inp.get("file_path") or "").lower()
        if any(k in fp for k in ("config", "settings", ".yaml", ".toml", ".ini", ".env")):
            cfg_hits += 1
    if total_edits >= 2 and cfg_hits / max(total_edits, 1) >= 0.6:
        return "meta", round(0.5 + 0.4 * (cfg_hits / total_edits), 3)

    # 6) refactor: Edit 위주, 테스트/커밋 없음, docs 아님
    if edit_like >= 3 and tests == 0 and commits == 0 and edit_ratio > read_ratio:
        return "refactor", round(min(1.0, 0.5 + edit_ratio * 0.5), 3)

    # 7) bugfix: Edit + test 실행, 커밋 없음
    if edit_like >= 1 and tests >= 1 and commits == 0:
        return "bugfix", 0.6

    return "mixed", 0.3


def score_jsonl(path: Path, config: dict, provider: str | None = None) -> dict:
    session_data = load_session(path, config, provider=provider)
    records = session_data["records"]
    partial = session_data["partial"]
    assistant_msgs = session_data["assistant_msgs"]
    user_msgs = session_data["user_msgs"]
    progress_msgs = session_data["progress_msgs"]
    system_msgs = session_data["system_msgs"]
    tool_names = session_data["tool_names"]
    tool_uses_with_input = session_data["tool_uses_with_input"]
    assistant_text_lc = session_data["assistant_text_lc"]
    user_text_lc = session_data["user_text_lc"]
    total_tool_calls = len(tool_names)

    schema_drift: list[str] = []
    if assistant_msgs and not any(safe_get(m, "message", "usage") for m in assistant_msgs):
        schema_drift.append("assistant.message.usage")
    if progress_msgs and not any(safe_get(m, "data", "type") for m in progress_msgs):
        schema_drift.append("progress.data.type")

    ctx = compute_context_efficiency(assistant_msgs)
    total_usd, total_output = compute_cost(assistant_msgs, config.get("model_rates", {}))
    ce_thr = config.get("cost_efficiency_thresholds", {"lo": 500.0, "hi": 50_000.0})
    cost_eff = compute_cost_efficiency(total_usd, total_output, ce_thr.get("lo", 500.0), ce_thr.get("hi", 50_000.0))
    focus = compute_role_focus(tool_names)
    re_thr = config.get("read_edit_ratio_thresholds", {"degraded": 2.0, "good": 6.0})
    read_edit, re_raw = compute_read_edit_ratio(tool_names, re_thr.get("degraded", 2.0), re_thr.get("good", 6.0))
    adherence = compute_constraint_adherence(user_msgs, system_msgs, total_tool_calls)
    hook = compute_hook_health(progress_msgs, system_msgs)
    bottleneck = compute_operational_bottleneck(system_msgs, assistant_msgs)

    useful_counts = count_useful_outputs(tool_uses_with_input)
    cpuo_thr = config.get("cost_per_useful_thresholds", {"lo": 10.0, "hi": 0.10})
    cost_per_useful = compute_cost_per_useful_output(total_usd, useful_counts["total"], cpuo_thr.get("lo", 10.0), cpuo_thr.get("hi", 0.10))
    rl_thr = config.get("reasoning_loop_thresholds", {"degraded_per_1k": 20.0, "good_per_1k": 10.0})
    reasoning_loop, rl_count, rl_per_1k = compute_reasoning_loop(
        assistant_text_lc, total_tool_calls,
        rl_thr.get("degraded_per_1k", 20.0), rl_thr.get("good_per_1k", 10.0),
    )
    sentiment, sentiment_stats = compute_sentiment(user_text_lc)
    session_type, session_type_conf = classify_session_type(
        tool_names, tool_uses_with_input, useful_counts, assistant_text_lc,
    )
    suppressed_axes = list(SESSION_TYPE_SUPPRESSIONS.get(session_type, ()))

    weights = config.get("scoring_weights", {})
    axes = {
        "context_efficiency": round(ctx, 4),
        "cost_efficiency": round(cost_eff, 4),
        "cost_per_useful_output": round(cost_per_useful, 4),
        "role_focus": round(focus, 4),
        "read_edit_ratio": round(read_edit, 4),
        "reasoning_loop": round(reasoning_loop, 4),
        "sentiment": round(sentiment, 4),
        "constraint_adherence": round(adherence, 4),
        "hook_health": round(hook, 4),
        "operational_bottleneck": round(bottleneck, 4),
    }
    weighted_avg = sum(axes[k] * weights.get(k, 1.0 / len(axes)) for k in axes)

    session_id = session_data["session_id"]
    project_dir = session_data["project_dir"]

    return {
        "axes": axes,
        "weighted_avg": round(weighted_avg, 4),
        "meta": {
            "sessionId": session_id,
            "provider": session_data.get("provider"),
            "project_dir": project_dir,
            "agent_attribution": session_data.get("agent_attribution") or {},
            "jsonl_lines": session_data["jsonl_lines"],
            "jsonl_bytes": session_data["jsonl_bytes"],
            "partial_score": partial,
            "schema_drift": schema_drift,
            "total_usd": round(total_usd, 6),
            "total_output_tokens": total_output,
            "total_tool_calls": total_tool_calls,
            "unique_tools": len(set(tool_names)),
            "read_edit_raw_ratio": round(re_raw, 3) if math.isfinite(re_raw) else None,
            "useful_outputs": useful_counts,
            "cost_per_useful_output_usd": (
                round(total_usd / useful_counts["total"], 4) if useful_counts["total"] > 0 else None
            ),
            "reasoning_loop_count": rl_count,
            "reasoning_loop_per_1k": round(rl_per_1k, 3),
            "sentiment_stats": sentiment_stats,
            "session_type": session_type,
            "session_type_confidence": session_type_conf,
            "suppressed_axes": suppressed_axes,
            "scored_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    }


def project_dir_key(cwd: str | None) -> str:
    if not cwd:
        return "UNKNOWN"
    return "-" + cwd.lstrip("/").replace("/", "-")


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Dashcam 7축 채점 (Claude Code JSONL)")
    parser.add_argument("--input", "-i", required=False, help="JSONL 파일 경로")
    parser.add_argument("input_pos", nargs="?", help="positional JSONL 경로 (argv[1])")
    parser.add_argument("--save", action="store_true", help="scores/ 에도 저장")
    parser.add_argument(
        "--provider",
        choices=("auto", "claude", "codex", "gemini"),
        default="auto",
        help="세션 로그 출처 (default: auto — path pattern + first-line sniff)",
    )
    args = parser.parse_args()

    jsonl_path = args.input or args.input_pos
    if not jsonl_path:
        print(json.dumps({"error": "JSONL 경로 필요 (--input 또는 positional)"}), file=sys.stderr)
        return 2
    path = Path(jsonl_path).expanduser()
    if not path.exists():
        print(json.dumps({"error": f"파일 없음: {path}"}), file=sys.stderr)
        return 2

    config = load_config()
    result = score_jsonl(path, config, provider=args.provider)

    if args.save:
        session_id = result["meta"]["sessionId"] or path.stem
        proj = project_dir_key(result["meta"]["project_dir"])
        scores_dir = AGENT_DASHCAM_ROOT / "scores"
        scores_dir.mkdir(parents=True, exist_ok=True)
        out_path = scores_dir / f"{proj}__{session_id}.json"
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2)
        result["meta"]["saved_to"] = str(out_path)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
