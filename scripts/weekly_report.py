#!/usr/bin/env python3
"""Agent Dashcam weekly report — 7일 윈도우 기반 주간 요약 + Slack payload.

daily 와 구분되는 weekly 전용 신호:
  - Week-over-week delta (이번 주 vs 지난 주 weighted_avg)
  - Combo 빈도 (단일 스냅샷이 아닌 세션별 발생 카운트)
  - Golden session 비율
  - 요일별 활동 sparkline
  - Best / worst 세션 픽

동작:
  1. scores/ 에서 지난 N일 (default 7) 안에 쓰인 점수 로드 (시간 기반)
  2. 그 이전 동일 길이 구간을 비교용으로 로드
  3. 10축 aggregate + weekly-specific 신호 계산
  4. Markdown → reports/weekly/weekly-YYYY-MM-DD.md
  5. Slack blocks → reports/weekly/weekly-YYYY-MM-DD.slack.json

사용법:
  python3 weekly_report.py                    # 7일 윈도우
  python3 weekly_report.py --days 14          # 14일
  python3 weekly_report.py --print-payload    # Slack JSON 만
  python3 weekly_report.py --stdout-md        # Markdown 만
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daily_report import (
    AGENT_DASHCAM_ROOT,
    AXIS_EMOJI,
    AXIS_ORDER,
    SCORES_DIR,
    _COMBO_PATTERNS,
    _aggregate_agent_attribution,
    _axis_actions,
    _filter_suppressed,
    _now_iso,
    _session_date,
    _suppression_tally,
    _weak_axes,
    _worst_sessions,
    bar,
    compute_axis_stats,
    load_config,
    render_subagent_breakdown_block,
    render_subagent_breakdown_md,
    render_worst_sessions_block,
    render_worst_sessions_md,
    trend_arrow,
)

REPORTS_DIR = AGENT_DASHCAM_ROOT / "reports" / "weekly"


def load_scores_in_window(end: datetime, days: int) -> list[dict]:
    """[end - days, end) 구간 안에 mtime 이 있는 점수 파일 로드."""
    if not SCORES_DIR.exists():
        return []
    cutoff_ts = (end - timedelta(days=days)).timestamp()
    end_ts = end.timestamp()
    out: list[dict] = []
    for p in SCORES_DIR.glob("*.json"):
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if not (cutoff_ts <= mt < end_ts):
            continue
        try:
            with open(p) as f:
                d = json.load(f)
            d["_path"] = str(p)
            d["_mtime"] = mt
            out.append(d)
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda d: d["_mtime"])
    return out


def sessions_by_day(scores: list[dict], end: datetime, days: int) -> list[tuple[str, int]]:
    """지난 N일 각 날짜별 세션 개수. 시간순 (오래된 날짜 먼저)."""
    buckets: dict[str, int] = {}
    for i in range(days - 1, -1, -1):
        key = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        buckets[key] = 0
    for s in scores:
        day = datetime.fromtimestamp(s["_mtime"], tz=timezone.utc).strftime("%Y-%m-%d")
        if day in buckets:
            buckets[day] += 1
    return list(buckets.items())


_SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def sparkline(counts: list[int]) -> str:
    if not counts:
        return ""
    mx = max(counts)
    if mx == 0:
        return _SPARK_CHARS[0] * len(counts)
    last = len(_SPARK_CHARS) - 1
    return "".join(_SPARK_CHARS[min(last, round(c / mx * last))] for c in counts)


def combo_frequency(scores: list[dict]) -> list[dict]:
    """세션별로 각 combo 패턴 hit 개수를 집계. daily 의 스냅샷 detect 와 구분."""
    freq: Counter[str] = Counter()
    labels: dict[str, str] = {p["id"]: p["label"] for p in _COMBO_PATTERNS}
    for s in scores:
        axes_raw = s.get("axes") or {}
        snapshot = {
            a: (axes_raw[a] if isinstance(axes_raw.get(a), (int, float)) else 1.0)
            for a in AXIS_ORDER
        }
        w = s.get("weighted_avg")
        snapshot["_weighted_avg"] = float(w) if isinstance(w, (int, float)) else 0.0
        for p in _COMBO_PATTERNS:
            try:
                if p["when"](snapshot):
                    freq[p["id"]] += 1
            except (TypeError, KeyError):
                continue
    n = len(scores)
    out: list[dict] = []
    for cid, count in freq.most_common():
        out.append({
            "id": cid,
            "label": labels.get(cid, cid),
            "count": count,
            "rate": round(count / n, 3) if n else 0,
        })
    return out


def golden_session_stats(scores: list[dict], threshold: float = 0.75) -> tuple[int, float]:
    hits = sum(
        1 for s in scores
        if isinstance(s.get("weighted_avg"), (int, float)) and s["weighted_avg"] >= threshold
    )
    n = len(scores)
    return hits, (round(hits / n, 3) if n else 0.0)


def _mean_weighted(scores: list[dict]) -> float | None:
    vals = [s.get("weighted_avg") for s in scores if isinstance(s.get("weighted_avg"), (int, float))]
    if not vals:
        return None
    return round(statistics.mean(vals), 4)


def wow_delta(this_avg: float | None, prev_avg: float | None) -> dict:
    if this_avg is None or prev_avg is None:
        return {"this": this_avg, "prev": prev_avg, "delta": None}
    return {"this": this_avg, "prev": prev_avg, "delta": round(this_avg - prev_avg, 4)}


def top_bottom_sessions(scores: list[dict], k: int = 1) -> tuple[list[dict], list[dict]]:
    ranked = [s for s in scores if isinstance(s.get("weighted_avg"), (int, float))]
    ranked.sort(key=lambda s: s["weighted_avg"])
    return ranked[-k:][::-1], ranked[:k]


def render_markdown(end: datetime, days: int, scores: list[dict], prev_scores: list[dict], stats: dict, wr_cfg: dict | None = None) -> str:
    period_end = end.strftime("%Y-%m-%d")
    period_start = (end - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    weighted = stats.get("_weighted_avg") or {}
    this_avg = _mean_weighted(scores)
    prev_avg = _mean_weighted(prev_scores)
    delta = wow_delta(this_avg, prev_avg)
    by_day = sessions_by_day(scores, end, days)
    counts = [c for _, c in by_day]
    spark = sparkline(counts)
    freq = combo_frequency(scores)
    golden_hits, golden_rate = golden_session_stats(scores)
    best, worst = top_bottom_sessions(scores, k=1)
    tally = _suppression_tally(scores)

    lines = [
        f"# Agent Dashcam weekly — {period_start} → {period_end}",
        "",
        f"- sessions this period: {len(scores)}  (prev period: {len(prev_scores)})",
        f"- activity by day: `{spark}` — " + ", ".join(f"{d}={c}" for d, c in by_day),
    ]
    if weighted:
        lines.append(f"- weighted_avg (window): **{weighted.get('avg')}** (slope {weighted.get('slope')})")
    if delta["delta"] is not None:
        arrow = trend_arrow(delta["delta"])
        sign = "+" if delta["delta"] >= 0 else ""
        lines.append(
            f"- week-over-week: {arrow} `{sign}{delta['delta']}` "
            f"(this={delta['this']}, prev={delta['prev']})"
        )
    lines.append(
        f"- golden sessions (weighted_avg ≥ 0.75): {golden_hits}/{len(scores)} "
        f"({int(golden_rate * 100)}%)"
    )
    if best:
        b = best[0]
        lines.append(f"- best session: weighted_avg={b.get('weighted_avg')} ({_session_date(b)})")
    if worst:
        w = worst[0]
        lines.append(f"- worst session: weighted_avg={w.get('weighted_avg')} ({_session_date(w)})")

    lines.append("")
    lines.append("## Axis stats (window avg)")
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

    if freq:
        lines.append("")
        lines.append("## Combo pattern frequency (this period)")
        for f in freq:
            lines.append(f"- **{f['label']}** × {f['count']} sessions ({int(f['rate'] * 100)}%)")

    weak, skipped = _filter_suppressed(
        _weak_axes(stats, threshold=0.4, top_n=5), tally, len(scores)
    )
    weak = weak[:3]
    if weak:
        lines.append("")
        lines.append("## Persistent weak axes (window avg < 0.4)")
        for name, avg in weak:
            lines.append("")
            lines.append(f"### `{name}` (window avg={avg})")
            for i, action in enumerate(_axis_actions(name), 1):
                lines.append(f"{i}. {action}")
    if skipped:
        lines.append("")
        lines.append(
            "> ℹ️ 세션 타입 특성상 자연 억제된 축: "
            + ", ".join(f"`{a}`" for a in skipped)
        )

    if wr_cfg and wr_cfg.get("show_worst_sessions", True):
        worst_list = _worst_sessions(
            scores,
            float(wr_cfg.get("worst_sessions_threshold", 0.5)),
            int(wr_cfg.get("worst_sessions_max", 3)),
        )
        lines.extend(render_worst_sessions_md(worst_list))

    if not wr_cfg or wr_cfg.get("show_subagent_breakdown", True):
        agg = _aggregate_agent_attribution(scores)
        top_n = int((wr_cfg or {}).get("subagent_breakdown_max", 5))
        lines.extend(render_subagent_breakdown_md(agg, top_n))

    return "\n".join(lines) + "\n"


def render_slack_payload(
    end: datetime,
    days: int,
    scores: list[dict],
    prev_scores: list[dict],
    stats: dict,
    channel: str,
    wr_cfg: dict | None = None,
) -> dict:
    period_end = end.strftime("%Y-%m-%d")
    period_start = (end - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    total_sessions = len(scores)
    weighted = stats.get("_weighted_avg") or {}
    this_avg = _mean_weighted(scores)
    prev_avg = _mean_weighted(prev_scores)
    delta = wow_delta(this_avg, prev_avg)
    by_day = sessions_by_day(scores, end, days)
    spark = sparkline([c for _, c in by_day])
    freq = combo_frequency(scores)
    golden_hits, golden_rate = golden_session_stats(scores)
    tally = _suppression_tally(scores)

    blocks: list[dict] = []
    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f":spiral_calendar_pad: Agent Dashcam weekly  {period_start} → {period_end}",
            "emoji": True,
        },
    })
    header_lines: list[str] = []
    if weighted:
        arrow = trend_arrow(weighted.get("slope", 0))
        header_lines.append(
            f"*weighted_avg:* `{weighted.get('avg')}` {arrow}  (n={total_sessions} sessions)"
        )
    if delta["delta"] is not None:
        d_arrow = trend_arrow(delta["delta"])
        sign = "+" if delta["delta"] >= 0 else ""
        header_lines.append(
            f"*week-over-week:* {d_arrow} `{sign}{delta['delta']}`  (prev {delta['prev']}, this {delta['this']})"
        )
    header_lines.append(f"*activity:* `{spark}`  ({total_sessions} sessions over {days}d)")
    header_lines.append(
        f"*golden sessions:* {golden_hits}/{total_sessions} ({int(golden_rate * 100)}%)"
    )
    if header_lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(header_lines)}})

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

    if freq:
        combo_lines = [":repeat: *Combo pattern frequency*"]
        for f in freq:
            combo_lines.append(
                f"• {f['label']} × *{f['count']}* sessions ({int(f['rate'] * 100)}%)"
            )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(combo_lines)}})

    weak, skipped = _filter_suppressed(
        _weak_axes(stats, threshold=0.4, top_n=5), tally, total_sessions
    )
    weak = weak[:3]
    if weak:
        action_lines = [":dart: *Persistent weak axes this week*"]
        for name, avg in weak:
            action_lines.append(f"\n*`{name}`* (window avg {avg})")
            for i, action in enumerate(_axis_actions(name), 1):
                action_lines.append(f"  {i}. {action}")
        if skipped:
            action_lines.append(
                "\n_ℹ️ 세션 타입상 자연 억제된 축은 생략: "
                + ", ".join(f"`{a}`" for a in skipped)
                + "_"
            )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(action_lines)}})
    elif skipped:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":white_check_mark: 주간 약축 모두 세션 타입상 자연 억제\n> "
                    + ", ".join(f"`{a}`" for a in skipped)
                ),
            },
        })

    if wr_cfg and wr_cfg.get("show_worst_sessions", True):
        worst_list = _worst_sessions(
            scores,
            float(wr_cfg.get("worst_sessions_threshold", 0.5)),
            int(wr_cfg.get("worst_sessions_max", 3)),
        )
        worst_block = render_worst_sessions_block(worst_list)
        if worst_block is not None:
            blocks.append(worst_block)

    if not wr_cfg or wr_cfg.get("show_subagent_breakdown", True):
        agg = _aggregate_agent_attribution(scores)
        top_n = int((wr_cfg or {}).get("subagent_breakdown_max", 5))
        breakdown_block = render_subagent_breakdown_block(agg, top_n)
        if breakdown_block is not None:
            blocks.append(breakdown_block)

    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": f"Agent Dashcam v3 · weekly · generated {_now_iso()} · window={days}d",
        }],
    })
    return {
        "channel": channel,
        "text": (
            f"Agent Dashcam weekly {period_start} → {period_end} "
            f"— weighted_avg={weighted.get('avg') if weighted else '?'}"
        ),
        "blocks": blocks,
    }


def write_report(end: datetime, md: str, payload: dict, dry_run: bool) -> dict:
    if dry_run:
        return {"dry_run": True}
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = end.strftime("%Y-%m-%d")
    paths: dict[str, str] = {}
    md_path = REPORTS_DIR / f"weekly-{date_str}.md"
    md_path.write_text(md)
    paths["md"] = str(md_path)
    payload_path = REPORTS_DIR / f"weekly-{date_str}.slack.json"
    payload_path.write_text(json.dumps(payload, indent=2))
    paths["slack_payload"] = str(payload_path)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Dashcam weekly report + Slack payload")
    parser.add_argument("--days", type=int, default=7, help="window length in days (default 7)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--print-payload", action="store_true", help="Slack blocks JSON만 stdout")
    parser.add_argument("--stdout-md", action="store_true", help="Markdown만 stdout")
    parser.add_argument("--channel", default=None, help="Slack channel override")
    args = parser.parse_args()

    config = load_config()
    wr_cfg = config.get("weekly_report") or config.get("daily_report") or {}
    channel = args.channel or wr_cfg.get("slack_channel") or "YOUR_SLACK_CHANNEL_ID"

    end = datetime.now(timezone.utc)
    this_window = load_scores_in_window(end, args.days)
    if not this_window:
        err = {"error": "no scores in window", "days": args.days, "scores_dir": str(SCORES_DIR)}
        print(json.dumps(err, indent=2), file=sys.stderr)
        return 1
    prev_window = load_scores_in_window(end - timedelta(days=args.days), args.days)

    stats = compute_axis_stats(this_window)
    md = render_markdown(end, args.days, this_window, prev_window, stats, wr_cfg)
    payload = render_slack_payload(end, args.days, this_window, prev_window, stats, channel, wr_cfg)

    if args.print_payload:
        print(json.dumps(payload, indent=2))
        return 0
    if args.stdout_md:
        print(md)
        return 0

    paths = write_report(end, md, payload, args.dry_run)
    summary = {
        "period_end": end.strftime("%Y-%m-%d"),
        "days": args.days,
        "sessions_this": len(this_window),
        "sessions_prev": len(prev_window),
        "weighted_avg_this": _mean_weighted(this_window),
        "weighted_avg_prev": _mean_weighted(prev_window),
        "channel": channel,
        "paths": paths,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
