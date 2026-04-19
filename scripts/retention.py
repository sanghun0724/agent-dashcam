#!/usr/bin/env python3
"""Agent Dashcam retention — scores/ 디렉토리 100건 유지 + 월간 summary + 동적 임계값 캘리브레이션.

동작:
  1. scores/ 의 JSON 파일을 mtime 기준 정렬
  2. limit 초과분은 월별로 그룹화 후 monthly/monthly-summary-YYYY-MM.json 에 merge
  3. 오래된 파일 삭제
  4. (v3) config.calibration.enabled 이고 30+ 세션 쌓이면 p20/p80 추출하여
     config.cost_efficiency_thresholds / cost_per_useful_thresholds 자동 업데이트

사용법:
  python3 retention.py              # 기본값 (config.json 의 scores_retention_limit)
  python3 retention.py --limit 50   # override
  python3 retention.py --dry-run    # 삭제 + 캘리브레이션 시뮬레이션
  python3 retention.py --calibrate-only  # 삭제 skip, 캘리브레이션만
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
from pathlib import Path


AGENT_DASHCAM_ROOT = Path(os.environ.get("AGENT_DASHCAM_ROOT") or (Path.home() / ".claude" / "agent-dashcam"))
CONFIG_PATH = AGENT_DASHCAM_ROOT / "config.json"
SCORES_DIR = AGENT_DASHCAM_ROOT / "scores"
MONTHLY_DIR = AGENT_DASHCAM_ROOT / "monthly"
LOG_PATH = AGENT_DASHCAM_ROOT / "logs" / "retention.log"


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def month_key(score: dict, fallback_path: Path) -> str:
    scored_at = score.get("meta", {}).get("scored_at") or ""
    if scored_at:
        try:
            dt = datetime.fromisoformat(scored_at.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m")
        except ValueError:
            pass
    ts = datetime.fromtimestamp(fallback_path.stat().st_mtime)
    return ts.strftime("%Y-%m")


def merge_monthly(month: str, scores: list[tuple[Path, dict]]) -> Path:
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    target = MONTHLY_DIR / f"monthly-summary-{month}.json"
    existing = {"month": month, "session_count": 0, "axes_stats": {}}
    if target.exists():
        try:
            with open(target) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    existing.setdefault("session_count", 0)
    existing.setdefault("axes_stats", {})
    existing["session_count"] += len(scores)

    per_axis: dict[str, list[float]] = defaultdict(list)
    for ax_name, stat in existing["axes_stats"].items():
        if "values" in stat:
            per_axis[ax_name].extend(stat.get("values", []))

    for _, score in scores:
        for ax_name, val in (score.get("axes") or {}).items():
            if isinstance(val, (int, float)):
                per_axis[ax_name].append(float(val))

    axes_stats: dict[str, dict] = {}
    for ax_name, vals in per_axis.items():
        if not vals:
            continue
        axes_stats[ax_name] = {
            "avg": round(statistics.mean(vals), 4),
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
            "count": len(vals),
            "values": vals[-500:],
        }
    existing["axes_stats"] = axes_stats
    existing["last_merged_at"] = _now_utc_iso()

    with open(target, "w") as f:
        json.dump(existing, f, indent=2)
    return target


def log_event(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = _now_utc_iso()
    with open(LOG_PATH, "a") as f:
        f.write(f"{ts} | {message}\n")


def _quantile(values: list[float], q: float) -> float:
    """stdlib only quantile (linear interpolation, type=7 compatible)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _load_recent_scores(window: int) -> list[dict]:
    """scores/ 에서 가장 최근 window 개 로드 (mtime 내림차순)."""
    if not SCORES_DIR.exists():
        return []
    files = sorted(SCORES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:window]
    out: list[dict] = []
    for p in files:
        try:
            with open(p) as f:
                out.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def calibrate_thresholds(config: dict, dry_run: bool = False) -> dict:
    """monthly-summary + 최근 scores 기반으로 임계값 p20/p80 자동 튜닝.

    대상:
      - cost_efficiency_thresholds: output_tokens / total_usd 의 p20, p80
      - cost_per_useful_thresholds: total_usd / useful_total 의 p20, p80 (역방향: lo가 더 큰 값)

    조건: config.calibration.enabled=True & 가용 샘플 >= min_sessions.
    Returns {"status": ..., "cost_efficiency": ..., "cost_per_useful": ...}
    """
    cal = config.get("calibration", {}) or {}
    if not cal.get("enabled", True):
        return {"status": "disabled"}
    min_sessions = int(cal.get("min_sessions", 30))
    window = int(cal.get("window", 90))
    q_lo = float(cal.get("quantile_lo", 0.20))
    q_hi = float(cal.get("quantile_hi", 0.80))

    scores = _load_recent_scores(window)
    if len(scores) < min_sessions:
        return {"status": "insufficient_samples", "have": len(scores), "need": min_sessions}

    output_per_dollar: list[float] = []
    cost_per_useful: list[float] = []
    for s in scores:
        meta = s.get("meta") or {}
        total_usd = meta.get("total_usd") or 0
        total_out = meta.get("total_output_tokens") or 0
        if isinstance(total_usd, (int, float)) and isinstance(total_out, (int, float)) and total_usd > 0 and total_out > 0:
            output_per_dollar.append(float(total_out) / float(total_usd))
        cpuo = meta.get("cost_per_useful_output_usd")
        if isinstance(cpuo, (int, float)) and cpuo > 0:
            cost_per_useful.append(float(cpuo))

    result: dict = {"status": "ok", "sample_count": len(scores)}
    now = _now_utc_iso()

    if output_per_dollar:
        p20 = _quantile(output_per_dollar, q_lo)
        p80 = _quantile(output_per_dollar, q_hi)
        if p20 > 0 and p80 > p20:
            result["cost_efficiency"] = {
                "lo": round(p20, 2),
                "hi": round(p80, 2),
                "prev_lo": config.get("cost_efficiency_thresholds", {}).get("lo"),
                "prev_hi": config.get("cost_efficiency_thresholds", {}).get("hi"),
                "samples": len(output_per_dollar),
            }
            if not dry_run:
                config.setdefault("cost_efficiency_thresholds", {})
                config["cost_efficiency_thresholds"]["lo"] = round(p20, 2)
                config["cost_efficiency_thresholds"]["hi"] = round(p80, 2)
                config["cost_efficiency_thresholds"]["auto_calibrated_at"] = now
                config["cost_efficiency_thresholds"]["source"] = (
                    f"auto-calibrated p{int(q_lo*100)}/p{int(q_hi*100)} "
                    f"from {len(output_per_dollar)} sessions"
                )

    if cost_per_useful:
        # 역방향: lo = 비싼 쪽 (p80 of raw $/output), hi = 저렴한 쪽 (p20)
        p20 = _quantile(cost_per_useful, q_lo)
        p80 = _quantile(cost_per_useful, q_hi)
        if p20 > 0 and p80 > p20:
            result["cost_per_useful"] = {
                "lo": round(p80, 4),
                "hi": round(p20, 4),
                "prev_lo": config.get("cost_per_useful_thresholds", {}).get("lo"),
                "prev_hi": config.get("cost_per_useful_thresholds", {}).get("hi"),
                "samples": len(cost_per_useful),
            }
            if not dry_run:
                config.setdefault("cost_per_useful_thresholds", {})
                config["cost_per_useful_thresholds"]["lo"] = round(p80, 4)
                config["cost_per_useful_thresholds"]["hi"] = round(p20, 4)
                config["cost_per_useful_thresholds"]["auto_calibrated_at"] = now
                config["cost_per_useful_thresholds"]["source"] = (
                    f"auto-calibrated p{int(q_hi*100)}/p{int(q_lo*100)} "
                    f"from {len(cost_per_useful)} sessions (inverse: lo=expensive)"
                )

    if not dry_run and ("cost_efficiency" in result or "cost_per_useful" in result):
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        log_event(f"calibration: updated thresholds from {len(scores)} samples")

    return result


def run_retention(limit: int, dry_run: bool, skip_calibration: bool = False) -> dict:
    config = load_config()
    if not SCORES_DIR.exists():
        result = {"deleted": 0, "kept": 0, "merged": {}, "reason": "scores dir missing"}
        if not skip_calibration:
            result["calibration"] = calibrate_thresholds(config, dry_run)
        return result

    score_files = [p for p in SCORES_DIR.iterdir() if p.is_file() and p.suffix == ".json"]
    score_files.sort(key=lambda p: p.stat().st_mtime)

    if len(score_files) <= limit:
        result = {"deleted": 0, "kept": len(score_files), "merged": {}, "reason": "under limit"}
        if not skip_calibration:
            result["calibration"] = calibrate_thresholds(config, dry_run)
        return result

    to_delete = score_files[: len(score_files) - limit]
    by_month: dict[str, list[tuple[Path, dict]]] = defaultdict(list)

    for p in to_delete:
        try:
            with open(p) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
        m = month_key(data, p)
        by_month[m].append((p, data))

    merged: dict[str, str] = {}
    for m, group in by_month.items():
        if dry_run:
            merged[m] = f"would merge {len(group)}"
        else:
            target = merge_monthly(m, group)
            merged[m] = str(target)

    deleted = 0
    if not dry_run:
        for p, _ in (item for g in by_month.values() for item in g):
            try:
                p.unlink()
                deleted += 1
            except OSError:
                continue

    log_event(f"retention run: limit={limit} deleted={deleted} kept={limit} merged={list(merged.keys())}")
    result = {"deleted": deleted, "kept": len(score_files) - deleted, "merged": merged}
    if not skip_calibration:
        # config 재로드 (방금 월간 merge 했으므로 최신 파일 상태로)
        result["calibration"] = calibrate_thresholds(load_config(), dry_run)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Dashcam scores retention + dynamic calibration")
    parser.add_argument("--limit", type=int, default=None, help="최대 유지 파일 수")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--calibrate-only", action="store_true", help="retention skip, 캘리브레이션만 실행")
    parser.add_argument("--skip-calibration", action="store_true", help="retention만, 캘리브레이션 skip")
    args = parser.parse_args()

    config = load_config()
    limit = args.limit if args.limit is not None else config.get("scores_retention_limit", 100)

    if args.calibrate_only:
        result = {"calibration": calibrate_thresholds(config, args.dry_run)}
    else:
        result = run_retention(limit, args.dry_run, skip_calibration=args.skip_calibration)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
