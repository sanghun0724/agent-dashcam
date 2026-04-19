#!/usr/bin/env python3
"""Agent Dashcam daily report — 최근 N개 세션 기반 일일 요약 + Slack payload 생성.

동작:
  1. scores/ 최근 window 개 로드 (config.daily_report.include_trend_window, default 7)
  2. 10축별 avg/min/max + 트렌드 슬로프 계산
  3. Markdown 리포트 -> reports/daily/daily-YYYY-MM-DD.md
  4. Slack blocks payload -> reports/daily/daily-YYYY-MM-DD.slack.json
  5. --print-payload: JSON만 stdout (MCP 툴로 송출용)
  6. --stdout-md: Markdown만 stdout

Slack 전송은 Python stdlib 제약상 직접 불가 — bin/agent-dashcam daily --send 가
payload JSON 경로를 표준 출력으로 안내하고 Claude Code가 MCP 툴 호출 책임.

사용법:
  python3 daily_report.py                     # 파일 저장 + 요약 출력
  python3 daily_report.py --window 14         # 14일치 트렌드
  python3 daily_report.py --print-payload     # Slack blocks JSON만
  python3 daily_report.py --stdout-md         # Markdown만
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


AGENT_DASHCAM_ROOT = Path(os.environ.get("AGENT_DASHCAM_ROOT") or (Path.home() / ".claude" / "agent-dashcam"))
CONFIG_PATH = AGENT_DASHCAM_ROOT / "config.json"
SCORES_DIR = AGENT_DASHCAM_ROOT / "scores"
REPORTS_DIR = AGENT_DASHCAM_ROOT / "reports" / "daily"

AXIS_ORDER = (
    "context_efficiency",
    "cost_efficiency",
    "cost_per_useful_output",
    "role_focus",
    "read_edit_ratio",
    "reasoning_loop",
    "sentiment",
    "constraint_adherence",
    "hook_health",
    "operational_bottleneck",
)

AXIS_EMOJI = {
    "context_efficiency": ":recycle:",
    "cost_efficiency": ":dollar:",
    "cost_per_useful_output": ":moneybag:",
    "role_focus": ":dart:",
    "read_edit_ratio": ":books:",
    "reasoning_loop": ":loop:",
    "sentiment": ":smiley:",
    "constraint_adherence": ":lock:",
    "hook_health": ":hook:",
    "operational_bottleneck": ":hourglass:",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_recent_scores(window: int) -> list[dict]:
    if not SCORES_DIR.exists():
        return []
    files = sorted(SCORES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:window]
    out: list[dict] = []
    for p in files:
        try:
            with open(p) as f:
                d = json.load(f)
            d["_path"] = str(p)
            d["_mtime"] = p.stat().st_mtime
            out.append(d)
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda d: d["_mtime"])
    return out


def linear_slope(values: list[float]) -> float:
    """단순 선형 회귀 기울기 — 최소제곱, index x 축."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    num = sum((xs[i] - mx) * (values[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return num / den


def compute_axis_stats(scores: list[dict]) -> dict:
    """축별 avg/min/max + trend slope."""
    per_axis: dict[str, list[float]] = {a: [] for a in AXIS_ORDER}
    weighted: list[float] = []
    for s in scores:
        axes = s.get("axes") or {}
        for a in AXIS_ORDER:
            v = axes.get(a)
            if isinstance(v, (int, float)):
                per_axis[a].append(float(v))
        w = s.get("weighted_avg")
        if isinstance(w, (int, float)):
            weighted.append(float(w))
    out: dict = {}
    for a, vals in per_axis.items():
        if not vals:
            out[a] = {"avg": None, "min": None, "max": None, "slope": 0.0, "n": 0}
            continue
        out[a] = {
            "avg": round(statistics.mean(vals), 4),
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
            "slope": round(linear_slope(vals), 4),
            "n": len(vals),
        }
    if weighted:
        out["_weighted_avg"] = {
            "avg": round(statistics.mean(weighted), 4),
            "min": round(min(weighted), 4),
            "max": round(max(weighted), 4),
            "slope": round(linear_slope(weighted), 4),
            "n": len(weighted),
        }
    return out


def bar(value: float, width: int = 10) -> str:
    if value is None:
        return "?" * width
    v = max(0.0, min(1.0, float(value)))
    filled = int(round(v * width))
    return "█" * filled + "░" * (width - filled)


def trend_arrow(slope: float) -> str:
    if slope > 0.02:
        return "↗"
    if slope < -0.02:
        return "↘"
    return "→"


def render_markdown(date: str, scores: list[dict], stats: dict, window: int) -> str:
    weighted = stats.get("_weighted_avg") or {}
    type_dist = _session_type_distribution(scores)
    lines = [
        f"# Agent Dashcam daily — {date}",
        "",
        f"- window: last {len(scores)}/{window} sessions",
        f"- period: {_session_date(scores[0]) if scores else '?'} → {_session_date(scores[-1]) if scores else '?'}",
    ]
    if type_dist:
        dist_str = ", ".join(f"{k}={v}" for k, v in sorted(type_dist.items(), key=lambda x: -x[1]))
        lines.append(f"- session types: {dist_str}")
    if weighted:
        arrow = trend_arrow(weighted.get("slope", 0))
        lines.append(
            f"- weighted_avg: **{weighted.get('avg')}** "
            f"(min={weighted.get('min')}, max={weighted.get('max')}, trend {arrow})"
        )
    lines.append("")
    lines.append("## Axis stats")
    lines.append("")
    lines.append("| Axis | avg | bar | min | max | trend |")
    lines.append("|------|-----|-----|-----|-----|-------|")
    for a in AXIS_ORDER:
        s = stats.get(a) or {}
        avg = s.get("avg")
        if avg is None:
            continue
        lines.append(
            f"| `{a}` | {avg} | `{bar(avg)}` | {s.get('min')} | {s.get('max')} | "
            f"{trend_arrow(s.get('slope', 0))} {s.get('slope')} |"
        )
    tally = _suppression_tally(scores)
    raw_weak = _weak_axes(stats, threshold=0.4, top_n=5)
    weak, skipped = _filter_suppressed(raw_weak, tally, len(scores))
    weak = weak[:3]
    if weak:
        lines.append("")
        lines.append("## Action items (weak axes)")
        for name, avg in weak:
            lines.append(f"")
            lines.append(f"### `{name}` (avg={avg})")
            for i, action in enumerate(_axis_actions(name), 1):
                lines.append(f"{i}. {action}")
    if skipped:
        lines.append("")
        lines.append(
            "> ℹ️ 세션 타입 특성상 자연히 낮은 축은 조치 생략: "
            + ", ".join(f"`{a}`" for a in skipped)
        )

    combos = _detect_combos(stats, tally, len(scores))
    if combos:
        lines.append("")
        lines.append("## Combo patterns detected")
        for c in combos:
            lines.append(f"- **{c['label']}** — {c['why']}. → {c['fix']}")
    return "\n".join(lines) + "\n"


def _session_date(score: dict) -> str:
    meta = score.get("meta") or {}
    sat = meta.get("scored_at") or ""
    if sat:
        try:
            return datetime.fromisoformat(sat.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.fromtimestamp(score.get("_mtime", 0)).strftime("%Y-%m-%d")


def _lowest_axis(stats: dict) -> tuple[str, float] | None:
    cand: list[tuple[str, float]] = []
    for a in AXIS_ORDER:
        s = stats.get(a) or {}
        avg = s.get("avg")
        if isinstance(avg, (int, float)):
            cand.append((a, avg))
    if not cand:
        return None
    cand.sort(key=lambda x: x[1])
    return cand[0]


_AXIS_SUGGESTIONS: dict[str, list[str]] = {
    "context_efficiency": [
        "긴 파일 반복 Read 지양, Grep → 필요 부분만 Read 로 좁히기",
        "탐색은 Explore agent 위임 (thoroughness=medium)",
        "긴 결과는 notepad_write_working 으로 컨텍스트 밀어내기",
    ],
    "cost_efficiency": [
        "단순 작업 Haiku 라우팅, 표준 작업 Sonnet, Opus 는 아키텍처 전용",
        "독립 tool call 한 메시지에 묶어 병렬 실행",
        "큰 tool 결과 재생성 반복 금지 (한번 받으면 캐싱)",
    ],
    "cost_per_useful_output": [
        "커밋 빈도↑ — 기능 단위로 쪼개 커밋",
        "테스트/빌드 실행을 세션 내 최소 1회 포함",
        "PR 생성까지 한 세션에서 마무리 (gh pr create)",
    ],
    "role_focus": [
        "executor/explore/architect 에이전트 위임 비중 확대",
        "skill 자동감지 키워드 활용 (/plan, /ralph, /deepsearch)",
        "Read heavy 패턴 깨기 — 한번에 여러 파일 말고 Grep 선행",
    ],
    "read_edit_ratio": [
        "ratio<2 (Edit 과다): 수정 전 관련 함수 Read 1~3개 습관화",
        "ratio>6 (Read 과다): 분석 마비 — Read 3개 이내에 Edit 1개 강제",
        "이상적 2~6 사이 (lucemia 실증값)",
    ],
    "reasoning_loop": [
        "/plan 또는 /ralplan 으로 합의된 계획 세운 후 실행",
        "한 가설씩 검증 — 병렬 수정 시도 금지",
        "근본원인 불분명시 tracer agent 로 인과 추적",
    ],
    "sentiment": [
        "가정을 먼저 말하고 AskUserQuestion 으로 확인받기",
        "애매한 요구사항은 Deep Interview 로 명확화",
        "디버깅 세션에서는 자연히 낮음 — 맥락 제공 필수",
    ],
    "constraint_adherence": [
        "--no-verify 절대 금지 (AGENTS.md 명시 제약)",
        "반복 위반 패턴은 custom hook 으로 차단",
        "logs/hook-errors.log 확인해 위반 종류 파악",
    ],
    "hook_health": [
        "logs/hook-errors.log 읽고 원인 파악",
        "문제 훅은 OMC_SKIP_HOOKS=... 로 일시 스킵",
        "Claude 버전업 후면 agent-dashcam envup 으로 영향 분석",
    ],
    "operational_bottleneck": [
        "독립 작업 병렬 실행 (한 메시지에 여러 tool call)",
        "긴 빌드/테스트는 run_in_background: true",
        "interactive 입력 대기 지양 — 비동기 전환",
    ],
}


_COMBO_PATTERNS: list[dict] = [
    {
        "id": "opus_overuse",
        "label": ":boom: Opus 남용 증후군",
        "when": lambda s: (s.get("cost_efficiency", 1) < 0.4) and (s.get("role_focus", 1) < 0.4),
        "why": "모든 걸 Opus 로 하고 agent 위임 부족",
        "fix": "단순 작업은 executor(model='haiku') 로 분리",
    },
    {
        "id": "analysis_paralysis",
        "label": ":mag: 분석 마비",
        "when": lambda s: (s.get("read_edit_ratio", 1) == 0 and s.get("cost_per_useful_output", 1) < 0.4),
        "why": "탐색만 하고 산출물 없음",
        "fix": "Read 3개 → Edit 1개 강제, 또는 /ralph 로 완료까지 지속",
    },
    {
        "id": "flailing",
        "label": ":tornado: 좌충우돌",
        "when": lambda s: (s.get("reasoning_loop", 1) < 0.4) and (s.get("sentiment", 1) < 0.4),
        "why": "계획 없이 시도 → 실패 → 재시도 루프",
        "fix": "작업 시작 전 /plan --consensus 필수화",
    },
    {
        "id": "env_rot",
        "label": ":warning: 환경 부패",
        "when": lambda s: (s.get("hook_health", 1) < 0.5) and (s.get("constraint_adherence", 1) < 0.5),
        "why": "훅 깨짐 or 규칙 파일 out of sync",
        "fix": "agent-dashcam envup → 영향 분석 → 훅/규칙 패치",
    },
    {
        "id": "golden",
        "label": ":trophy: 황금 세션",
        "when": lambda s: all(s.get(a, 0) >= 0.6 for a in AXIS_ORDER) and s.get("_weighted_avg", 0) >= 0.75,
        "why": "환경+판단+실행 모두 균형",
        "fix": "progress.txt 에 이 패턴 기록 → 재현 가능하게",
    },
]


def _axis_actions(axis: str) -> list[str]:
    v = _AXIS_SUGGESTIONS.get(axis)
    if not v:
        return ["axis 검토 필요"]
    return v


def _axis_suggestion(axis: str) -> str:
    # 첫 번째 조치를 단문으로 반환 (하위호환)
    return _axis_actions(axis)[0]


_COMBO_REQUIRED_AXES: dict[str, tuple[str, ...]] = {
    "opus_overuse": ("cost_efficiency", "role_focus"),
    "analysis_paralysis": ("read_edit_ratio", "cost_per_useful_output"),
    "flailing": ("reasoning_loop", "sentiment"),
    "env_rot": ("hook_health", "constraint_adherence"),
    "golden": (),
}


def _detect_combos(
    stats: dict,
    tally: dict[str, int] | None = None,
    total_sessions: int = 0,
) -> list[dict]:
    """축 avg 에서 콤보 패턴 탐지. 세션 타입상 억제된 축이 포함된 콤보는 skip."""
    snapshot = {a: (stats.get(a) or {}).get("avg") for a in AXIS_ORDER}
    weighted = (stats.get("_weighted_avg") or {}).get("avg")
    if weighted is not None:
        snapshot["_weighted_avg"] = weighted
    clean = {k: (v if isinstance(v, (int, float)) else 1.0) for k, v in snapshot.items()}
    hits = []
    tally = tally or {}
    for p in _COMBO_PATTERNS:
        # 억제 체크: 이 콤보가 필요로 하는 축 중 하나라도 세션 과반이 억제면 skip
        required = _COMBO_REQUIRED_AXES.get(p["id"], ())
        if total_sessions and any(
            tally.get(a, 0) / total_sessions >= 0.5 for a in required
        ):
            continue
        try:
            if p["when"](clean):
                hits.append({"id": p["id"], "label": p["label"], "why": p["why"], "fix": p["fix"]})
        except (TypeError, KeyError):
            continue
    return hits


def _weak_axes(stats: dict, threshold: float = 0.4, top_n: int = 3) -> list[tuple[str, float]]:
    weak: list[tuple[str, float]] = []
    for a in AXIS_ORDER:
        s = stats.get(a) or {}
        avg = s.get("avg")
        if isinstance(avg, (int, float)) and avg < threshold:
            weak.append((a, avg))
    weak.sort(key=lambda x: x[1])
    return weak[:top_n]


def _suppression_tally(scores: list[dict]) -> dict[str, int]:
    """축별로 '억제 세션' 개수 집계 — 다수가 억제라면 해당 축 조치 생략."""
    out: dict[str, int] = {a: 0 for a in AXIS_ORDER}
    for s in scores:
        meta = s.get("meta") or {}
        for a in (meta.get("suppressed_axes") or []):
            if a in out:
                out[a] += 1
    return out


def _filter_suppressed(
    axes: list[tuple[str, float]],
    suppression_tally: dict[str, int],
    total_sessions: int,
    strict_majority: float = 0.5,
) -> tuple[list[tuple[str, float]], list[str]]:
    """세션 절반 이상이 억제 대상이면 skip. (kept, skipped_names) 반환."""
    kept: list[tuple[str, float]] = []
    skipped: list[str] = []
    for name, avg in axes:
        tally = suppression_tally.get(name, 0)
        if total_sessions and tally / total_sessions >= strict_majority:
            skipped.append(name)
        else:
            kept.append((name, avg))
    return kept, skipped


def _session_type_distribution(scores: list[dict]) -> dict[str, int]:
    dist: Counter[str] = Counter()
    for s in scores:
        t = ((s.get("meta") or {}).get("session_type")) or "unknown"
        dist[t] += 1
    return dict(dist)


def render_slack_payload(date: str, scores: list[dict], stats: dict, channel: str) -> dict:
    weighted = stats.get("_weighted_avg") or {}
    total_sessions = len(scores)
    tally = _suppression_tally(scores)
    type_dist = _session_type_distribution(scores)
    blocks: list[dict] = []
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f":chart_with_upwards_trend: Agent Dashcam {date}", "emoji": True},
    })
    if weighted:
        arrow = trend_arrow(weighted.get("slope", 0))
        header_lines = [
            f"*weighted_avg*: `{weighted.get('avg')}` {arrow} "
            f"(min {weighted.get('min')} / max {weighted.get('max')}, "
            f"slope {weighted.get('slope')}, n={weighted.get('n')})"
        ]
        if type_dist:
            dist_str = ", ".join(f"`{k}`×{v}" for k, v in sorted(type_dist.items(), key=lambda x: -x[1]))
            header_lines.append(f"_session types:_ {dist_str}")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(header_lines)},
        })
    rows: list[str] = []
    for a in AXIS_ORDER:
        s = stats.get(a) or {}
        avg = s.get("avg")
        if avg is None:
            continue
        em = AXIS_EMOJI.get(a, ":small_blue_diamond:")
        arrow = trend_arrow(s.get("slope", 0))
        rows.append(f"{em} `{a:<24}` {bar(avg)} {avg} {arrow}")
    if rows:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "```\n" + "\n".join(rows) + "\n```"},
        })
    raw_weak = _weak_axes(stats, threshold=0.4, top_n=5)
    weak, skipped = _filter_suppressed(raw_weak, tally, total_sessions)
    weak = weak[:3]
    if weak:
        action_lines = [":warning: *Action items* — weak axes (avg < 0.4)"]
        for name, avg in weak:
            action_lines.append(f"\n*`{name}`* (avg {avg})")
            for i, action in enumerate(_axis_actions(name), 1):
                action_lines.append(f"  {i}. {action}")
        if skipped:
            action_lines.append(
                "\n_ℹ️ 세션 타입 특성상 자연히 낮은 축은 조치 생략: "
                + ", ".join(f"`{a}`" for a in skipped)
                + "_"
            )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(action_lines)},
        })
    elif skipped:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":white_check_mark: *Action* — weak axes 모두 세션 타입상 자연 억제\n"
                    "> " + ", ".join(f"`{a}`" for a in skipped)
                ),
            },
        })
    else:
        lowest = _lowest_axis(stats)
        if lowest:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":white_check_mark: *Action* — 가장 약한 축 `{lowest[0]}` (avg {lowest[1]})\n"
                        f"> {_axis_suggestion(lowest[0])}"
                    ),
                },
            })
    combos = _detect_combos(stats, tally, total_sessions)
    if combos:
        combo_lines = [":mag_right: *Combo patterns*"]
        for c in combos:
            combo_lines.append(f"• {c['label']} — {c['why']}\n   → _{c['fix']}_")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(combo_lines)},
        })
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": f"Agent Dashcam v3 · generated {_now_iso()} · sessions={total_sessions}",
        }],
    })
    return {
        "channel": channel,
        "text": f"Agent Dashcam daily {date} — weighted_avg={weighted.get('avg') if weighted else '?'}",
        "blocks": blocks,
    }


def write_report(date: str, md: str, payload: dict, dry_run: bool) -> dict:
    paths: dict[str, str] = {}
    if dry_run:
        return {"dry_run": True}
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORTS_DIR / f"daily-{date}.md"
    with open(md_path, "w") as f:
        f.write(md)
    paths["md"] = str(md_path)
    payload_path = REPORTS_DIR / f"daily-{date}.slack.json"
    with open(payload_path, "w") as f:
        json.dump(payload, f, indent=2)
    paths["slack_payload"] = str(payload_path)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Dashcam daily report + Slack payload")
    parser.add_argument("--window", type=int, default=None, help="최근 N개 세션 (default config.daily_report.include_trend_window or 7)")
    parser.add_argument("--dry-run", action="store_true", help="파일 저장 skip")
    parser.add_argument("--print-payload", action="store_true", help="Slack blocks JSON만 stdout")
    parser.add_argument("--stdout-md", action="store_true", help="Markdown만 stdout")
    parser.add_argument("--channel", default=None, help="Slack channel override (default config.daily_report.slack_channel)")
    args = parser.parse_args()

    config = load_config()
    dr_cfg = config.get("daily_report", {}) or {}
    window = args.window or int(dr_cfg.get("include_trend_window", 7))
    channel = args.channel or dr_cfg.get("slack_channel") or "YOUR_SLACK_CHANNEL_ID"

    scores = load_recent_scores(window)
    if not scores:
        err = {"error": "no scores found", "scores_dir": str(SCORES_DIR)}
        print(json.dumps(err, indent=2), file=sys.stderr)
        return 1

    stats = compute_axis_stats(scores)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    md = render_markdown(date, scores, stats, window)
    payload = render_slack_payload(date, scores, stats, channel)

    if args.print_payload:
        print(json.dumps(payload, indent=2))
        return 0
    if args.stdout_md:
        print(md)
        return 0

    paths = write_report(date, md, payload, args.dry_run)
    summary = {
        "date": date,
        "window": window,
        "sessions": len(scores),
        "channel": channel,
        "weighted_avg": (stats.get("_weighted_avg") or {}).get("avg"),
        "lowest_axis": (_lowest_axis(stats) or (None,))[0],
        "paths": paths,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
