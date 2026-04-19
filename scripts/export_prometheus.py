#!/usr/bin/env python3
"""Agent Dashcam Prometheus exporter — textfile exposition format.

node_exporter textfile collector와 호환되는 포맷으로 agent-dashcam 점수를 노출.
Grafana/Prometheus에서 시계열 대시보드로 시각화.

메트릭:
  agent_dashcam_latest_axis_score{axis}         — 가장 최근 세션의 축 점수
  agent_dashcam_latest_weighted_avg             — 가장 최근 세션의 가중 평균
  agent_dashcam_avg_axis_score_window{axis}     — N일 창 평균 (default 7)
  agent_dashcam_slope_axis_window{axis}         — N일 창 선형 회귀 기울기
  agent_dashcam_total_scored_sessions           — scores/ 디렉토리 총 세션 수
  agent_dashcam_schema_drift_recent             — 최근 N개 중 드리프트 발생 세션
  agent_dashcam_threshold{axis,kind}            — 동적 캘리브레이션 임계값
  agent_dashcam_last_export_timestamp_seconds   — 이 export 실행 시각

사용법:
  python3 export_prometheus.py                        # /tmp/agent-dashcam.prom
  python3 export_prometheus.py --output /var/lib/node_exporter/textfile/agent-dashcam.prom
  python3 export_prometheus.py --stdout                # stdout
  python3 export_prometheus.py --window 30             # 30일 창 평균
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path


AGENT_DASHCAM_ROOT = Path(os.environ.get("AGENT_DASHCAM_ROOT") or (Path.home() / ".claude" / "agent-dashcam"))
CONFIG_PATH = AGENT_DASHCAM_ROOT / "config.json"
SCORES_DIR = AGENT_DASHCAM_ROOT / "scores"
DEFAULT_OUTPUT = Path("/tmp/agent-dashcam.prom")


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


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_scores(window: int) -> tuple[list[dict], int]:
    if not SCORES_DIR.exists():
        return [], 0
    all_files = sorted(SCORES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    total = len(all_files)
    out: list[dict] = []
    for p in all_files[:window]:
        try:
            with open(p) as f:
                out.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return out, total


def slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    num = sum((xs[i] - mx) * (values[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metric(name: str, labels: dict | None, value: float) -> str:
    if labels:
        lbl = ",".join(f'{k}="{_escape(str(v))}"' for k, v in labels.items())
        return f'{name}{{{lbl}}} {value}'
    return f"{name} {value}"


def render_exposition(scores: list[dict], total_sessions: int, config: dict, window: int) -> str:
    lines: list[str] = []
    ts = int(time.time())
    lines.append("# HELP agent_dashcam_last_export_timestamp_seconds Unix timestamp of last export")
    lines.append("# TYPE agent_dashcam_last_export_timestamp_seconds gauge")
    lines.append(metric("agent_dashcam_last_export_timestamp_seconds", None, ts))

    lines.append("# HELP agent_dashcam_total_scored_sessions Total score files in scores/")
    lines.append("# TYPE agent_dashcam_total_scored_sessions gauge")
    lines.append(metric("agent_dashcam_total_scored_sessions", None, total_sessions))

    if not scores:
        return "\n".join(lines) + "\n"

    latest = scores[0]
    axes = latest.get("axes") or {}
    latest_w = latest.get("weighted_avg")
    if isinstance(latest_w, (int, float)):
        lines.append("# HELP agent_dashcam_latest_weighted_avg Latest session weighted average score")
        lines.append("# TYPE agent_dashcam_latest_weighted_avg gauge")
        lines.append(metric("agent_dashcam_latest_weighted_avg", None, latest_w))

    lines.append("# HELP agent_dashcam_latest_axis_score Latest session per-axis score")
    lines.append("# TYPE agent_dashcam_latest_axis_score gauge")
    for a in AXIS_ORDER:
        v = axes.get(a)
        if isinstance(v, (int, float)):
            lines.append(metric("agent_dashcam_latest_axis_score", {"axis": a}, v))

    per_axis: dict[str, list[float]] = {a: [] for a in AXIS_ORDER}
    weighted: list[float] = []
    for s in scores:
        ax = s.get("axes") or {}
        for a in AXIS_ORDER:
            v = ax.get(a)
            if isinstance(v, (int, float)):
                per_axis[a].append(float(v))
        w = s.get("weighted_avg")
        if isinstance(w, (int, float)):
            weighted.append(float(w))

    lines.append(f"# HELP agent_dashcam_avg_axis_score_window Axis average over last {window} sessions")
    lines.append("# TYPE agent_dashcam_avg_axis_score_window gauge")
    for a, vals in per_axis.items():
        if vals:
            lines.append(metric("agent_dashcam_avg_axis_score_window", {"axis": a, "window": str(window)}, round(statistics.mean(vals), 4)))

    lines.append(f"# HELP agent_dashcam_slope_axis_window Linear slope over last {window} sessions")
    lines.append("# TYPE agent_dashcam_slope_axis_window gauge")
    for a, vals in per_axis.items():
        if len(vals) >= 2:
            lines.append(metric("agent_dashcam_slope_axis_window", {"axis": a, "window": str(window)}, round(slope(list(reversed(vals))), 4)))

    if weighted:
        lines.append("# HELP agent_dashcam_avg_weighted_avg_window Weighted average mean over window")
        lines.append("# TYPE agent_dashcam_avg_weighted_avg_window gauge")
        lines.append(metric("agent_dashcam_avg_weighted_avg_window", {"window": str(window)}, round(statistics.mean(weighted), 4)))

    drift_sessions = 0
    for s in scores:
        meta = s.get("meta") or {}
        if meta.get("schema_drift"):
            drift_sessions += 1
    lines.append(f"# HELP agent_dashcam_schema_drift_recent Sessions with schema drift (last {window})")
    lines.append("# TYPE agent_dashcam_schema_drift_recent gauge")
    lines.append(metric("agent_dashcam_schema_drift_recent", {"window": str(window)}, drift_sessions))

    # thresholds
    lines.append("# HELP agent_dashcam_threshold Dynamic calibration threshold")
    lines.append("# TYPE agent_dashcam_threshold gauge")
    for key, axis in (
        ("cost_efficiency_thresholds", "cost_efficiency"),
        ("cost_per_useful_thresholds", "cost_per_useful_output"),
        ("read_edit_ratio_thresholds", "read_edit_ratio"),
    ):
        thr = config.get(key) or {}
        for k in ("lo", "hi"):
            v = thr.get(k)
            if isinstance(v, (int, float)):
                lines.append(metric("agent_dashcam_threshold", {"axis": axis, "kind": k}, v))
        # reasoning_loop_thresholds uses different keys
    rl = config.get("reasoning_loop_thresholds") or {}
    for k in ("degraded_per_1k", "good_per_1k"):
        v = rl.get(k)
        if isinstance(v, (int, float)):
            lines.append(metric("agent_dashcam_threshold", {"axis": "reasoning_loop", "kind": k}, v))
    sent = config.get("sentiment_thresholds") or {}
    for k in ("degraded_ratio", "good_ratio"):
        v = sent.get(k)
        if isinstance(v, (int, float)):
            lines.append(metric("agent_dashcam_threshold", {"axis": "sentiment", "kind": k}, v))

    # per-axis weights
    weights = config.get("scoring_weights") or {}
    if weights:
        lines.append("# HELP agent_dashcam_scoring_weight Configured scoring weight per axis")
        lines.append("# TYPE agent_dashcam_scoring_weight gauge")
        for a, w in weights.items():
            if isinstance(w, (int, float)):
                lines.append(metric("agent_dashcam_scoring_weight", {"axis": a}, w))

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Dashcam Prometheus textfile exporter")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="출력 경로 (default /tmp/agent-dashcam.prom)")
    parser.add_argument("--window", type=int, default=30, help="창 크기 세션 수 (default 30)")
    parser.add_argument("--stdout", action="store_true", help="파일 대신 stdout")
    args = parser.parse_args()

    config = load_config()
    scores, total = load_scores(args.window)
    body = render_exposition(scores, total, config, args.window)

    if args.stdout:
        sys.stdout.write(body)
        return 0

    out = Path(args.output)
    # atomic write
    tmp = out.with_suffix(out.suffix + ".tmp")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w") as f:
        f.write(body)
    tmp.replace(out)
    print(json.dumps({
        "output": str(out),
        "bytes": len(body),
        "sessions_in_window": len(scores),
        "total_sessions": total,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
