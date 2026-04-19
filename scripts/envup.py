#!/usr/bin/env python3
"""Env-Up — 외부 변화 감지 파이프라인 (5단계).

단계:
  1. claude --version snapshot
  2. GitHub releases fetch (urllib, stdlib only)
  3. known-issues.json 3-tier matching:
     (a) github_issue API closure 체크
     (b) release_keyword 정확 매칭 (릴리즈 노트 body에)
     (c) tags fuzzy (NEEDS_USER_CONFIRM 마킹)
  4. workaround_file 존재 검증 (없으면 FILE_MISSING 태그)
  5. 리포트 생성 (reports/envup-YYYY-MM-DD.md) + known-issues.json 갱신

네트워크 장애 시: offline 모드 — 리포트에 OFFLINE 마킹, 매칭 skip.

사용법:
  python3 envup.py              # full run
  python3 envup.py --dry-run    # 파일 갱신 없이
  python3 envup.py --offline    # 네트워크 호출 skip
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


AGENT_DASHCAM_ROOT = Path(os.environ.get("AGENT_DASHCAM_ROOT") or (Path.home() / ".claude" / "agent-dashcam"))
CONFIG_PATH = AGENT_DASHCAM_ROOT / "config.json"
ISSUES_PATH = AGENT_DASHCAM_ROOT / "known-issues.json"
REPORTS_DIR = AGENT_DASHCAM_ROOT / "reports"
SCORES_DIR = AGENT_DASHCAM_ROOT / "scores"
GITHUB_API = "https://api.github.com"
UA = "agent-dashcam-envup/0.1"


def scan_schema_drift(window: int = 30) -> dict:
    """scores/ 최근 window 개에서 meta.schema_drift non-empty 세션 카운트.

    Returns {
      "scanned": N, "sessions_with_drift": M,
      "drift_fields": {field: count}, "recent": [{sessionId, drift}],
    }
    """
    if not SCORES_DIR.exists():
        return {"scanned": 0, "sessions_with_drift": 0, "drift_fields": {}, "recent": []}
    files = sorted(SCORES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:window]
    drift_counter: dict[str, int] = {}
    sessions_with_drift = 0
    recent: list[dict] = []
    for p in files:
        try:
            with open(p) as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        meta = d.get("meta") or {}
        drift = meta.get("schema_drift") or []
        if drift:
            sessions_with_drift += 1
            for field in drift:
                drift_counter[field] = drift_counter.get(field, 0) + 1
            if len(recent) < 5:
                recent.append({
                    "sessionId": (meta.get("sessionId") or "?")[:8],
                    "drift": drift,
                    "scored_at": meta.get("scored_at"),
                })
    return {
        "scanned": len(files),
        "sessions_with_drift": sessions_with_drift,
        "drift_fields": drift_counter,
        "recent": recent,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def claude_version() -> str:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
        raw = (out.stdout + out.stderr).strip().split("\n")[0]
        m = re.search(r"\d+\.\d+\.\d+(?:[-+][\w.]+)?", raw)
        return m.group(0) if m else (raw or "unknown")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "unavailable"


def http_get(url: str, timeout: int = 10) -> dict | list | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_releases(repo: str, limit: int = 10) -> tuple[list, str | None]:
    url = f"{GITHUB_API}/repos/{repo}/releases?per_page={limit}"
    try:
        data = http_get(url)
        if isinstance(data, list):
            return data, None
        return [], "unexpected response shape"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return [], f"network_error: {e}"


def fetch_issue(repo: str, issue_num: int) -> tuple[dict | None, str | None]:
    url = f"{GITHUB_API}/repos/{repo}/issues/{issue_num}"
    try:
        data = http_get(url)
        if isinstance(data, dict):
            return data, None
        return None, "unexpected response shape"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return None, f"network_error: {e}"


def load_issues() -> list:
    try:
        with open(ISSUES_PATH) as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_issues(issues: list) -> None:
    with open(ISSUES_PATH, "w") as f:
        json.dump(issues, f, indent=2)
        f.write("\n")


def expand_path(p: str | None) -> Path | None:
    if not p:
        return None
    return Path(os.path.expandvars(os.path.expanduser(p)))


def match_tier1_github_issue(issue: dict, repo: str, offline: bool) -> dict | None:
    """Tier 1: github_issue 번호가 있으면 API closure 체크."""
    num = issue.get("github_issue")
    if not num or offline:
        return None
    data, err = fetch_issue(repo, num)
    if err:
        return {"tier": 1, "status": "api_error", "detail": err}
    state = data.get("state") if data else None
    if state == "closed":
        return {"tier": 1, "status": "resolved", "evidence": f"issue #{num} closed at {data.get('closed_at')}"}
    return {"tier": 1, "status": "active", "evidence": f"issue #{num} state={state}"}


def match_tier2_release_keyword(issue: dict, releases: list) -> dict | None:
    """Tier 2: release_keyword가 릴리즈 노트 body에 정확히 포함되면 resolved 후보."""
    kw = issue.get("release_keyword")
    if not kw:
        return None
    kw_lc = kw.lower()
    for rel in releases:
        body = (rel.get("body") or "").lower()
        name = (rel.get("name") or "").lower()
        tag = (rel.get("tag_name") or "").lower()
        if kw_lc in body or kw_lc in name:
            return {
                "tier": 2,
                "status": "resolved_candidate",
                "evidence": f"keyword '{kw}' found in release {tag}",
                "version": rel.get("tag_name"),
            }
    return {"tier": 2, "status": "no_match", "evidence": f"keyword '{kw}' not in recent {len(releases)} releases"}


def match_tier3_tags_fuzzy(issue: dict, releases: list) -> dict | None:
    """Tier 3: tags fuzzy match — NEEDS_USER_CONFIRM로 마킹."""
    tags = issue.get("tags") or []
    if not tags:
        return None
    hits = []
    for rel in releases:
        body = (rel.get("body") or "").lower()
        name = (rel.get("name") or "").lower()
        for t in tags:
            if not isinstance(t, str):
                continue
            if t.lower() in body or t.lower() in name:
                hits.append({"release": rel.get("tag_name"), "tag": t})
    if hits:
        return {"tier": 3, "status": "NEEDS_USER_CONFIRM", "evidence": f"tag matches: {hits[:3]}"}
    return {"tier": 3, "status": "no_match", "evidence": f"no tag matches in recent releases"}


def validate_workaround(issue: dict) -> list[str]:
    tags = []
    wf = issue.get("workaround_file")
    if wf:
        p = expand_path(wf)
        if p and not p.exists():
            tags.append("FILE_MISSING")
    return tags


def run_envup(offline: bool, dry_run: bool, config: dict) -> dict:
    started = _now_iso()
    version = claude_version()

    repo = config.get("github_repo", "anthropics/claude-code")
    releases: list = []
    release_err: str | None = None
    if not offline:
        releases, release_err = fetch_releases(repo, limit=10)
        if release_err:
            offline = True  # degrade to offline for rest of run

    issues = load_issues()
    results = []
    updated_any = False
    for iss in issues:
        r = {
            "id": iss["id"],
            "title": iss["title"],
            "prior_status": iss.get("status"),
            "match": None,
            "extra_tags": [],
            "version_checked": version,
        }
        t1 = match_tier1_github_issue(iss, repo, offline)
        if t1 and t1.get("status") == "resolved":
            r["match"] = t1
            iss["status"] = "resolved"
            iss["verified_in_version"] = version
            updated_any = True
        else:
            t2 = match_tier2_release_keyword(iss, releases)
            if t2 and t2.get("status") == "resolved_candidate":
                r["match"] = t2
                iss["status"] = "NEEDS_USER_CONFIRM"
                iss["verified_in_version"] = t2.get("version") or version
                updated_any = True
            else:
                t3 = match_tier3_tags_fuzzy(iss, releases)
                r["match"] = t3 or t2 or t1 or {"tier": 0, "status": "skipped"}
        iss["last_checked_version"] = version
        iss["last_checked_at"] = started
        extra = validate_workaround(iss)
        r["extra_tags"] = extra
        if extra:
            existing = set(iss.get("tags") or [])
            iss["tags"] = sorted(existing | set(extra))
            updated_any = True
        results.append(r)

    suggestions = []
    for rel in releases[:3]:
        body = rel.get("body", "") or ""
        for kw in ("hook", "session", "settings", "subagent"):
            if kw in body.lower():
                suggestions.append({"release": rel.get("tag_name"), "keyword": kw})
                break

    drift_window = int(config.get("envup", {}).get("schema_drift_window", 30))
    drift_report = scan_schema_drift(window=drift_window)

    summary = {
        "started_at": started,
        "version": version,
        "repo": repo,
        "offline": offline,
        "release_error": release_err,
        "releases_scanned": len(releases),
        "issues_total": len(issues),
        "issues_resolved": sum(1 for r in results if r["match"] and r["match"].get("status") == "resolved"),
        "needs_user_confirm": sum(1 for r in results if r["match"] and r["match"].get("status") in ("NEEDS_USER_CONFIRM", "resolved_candidate")),
        "suggestions": suggestions,
        "results": results,
        "schema_drift": drift_report,
    }

    # last_checked_version/at은 매 실행마다 갱신되므로 항상 저장
    if not dry_run:
        save_issues(issues)

    report_path = write_report(summary, dry_run)
    summary["report_path"] = str(report_path) if report_path else None
    return summary


def write_report(summary: dict, dry_run: bool) -> Path | None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = REPORTS_DIR / f"envup-{date}.md"
    lines = []
    lines.append(f"# Env-Up report — {date}")
    lines.append("")
    lines.append(f"- started: {summary['started_at']}")
    lines.append(f"- claude version: `{summary['version']}`")
    lines.append(f"- repo: `{summary['repo']}`")
    lines.append(f"- mode: {'OFFLINE' if summary['offline'] else 'online'}")
    if summary.get("release_error"):
        lines.append(f"- release fetch error: `{summary['release_error']}`")
    lines.append(f"- releases scanned: {summary['releases_scanned']}")
    lines.append("")
    lines.append(f"## Summary")
    lines.append(f"- total issues: {summary['issues_total']}")
    lines.append(f"- resolved: {summary['issues_resolved']}")
    lines.append(f"- needs confirm: {summary['needs_user_confirm']}")
    lines.append("")

    drift = summary.get("schema_drift") or {}
    if drift.get("sessions_with_drift", 0) > 0:
        lines.append("## ⚠️ Schema Drift Warning")
        lines.append(f"- scanned: last {drift['scanned']} sessions")
        lines.append(f"- sessions with drift: **{drift['sessions_with_drift']}**")
        lines.append(f"- drift fields (count):")
        for field, cnt in sorted(drift.get("drift_fields", {}).items(), key=lambda x: -x[1]):
            lines.append(f"  - `{field}`: {cnt}")
        if drift.get("recent"):
            lines.append(f"- recent drift samples:")
            for r in drift["recent"]:
                sid = r.get("sessionId") or "?"
                fields = ",".join(r.get("drift") or [])
                lines.append(f"  - `{sid}` → {fields}")
        lines.append("")
        lines.append("**Action**: Claude Code JSONL 스키마 변경 가능성. `agent_dashcam_score.py` 필드 매핑 검토 필요.")
        lines.append("")
    else:
        scanned = drift.get("scanned", 0)
        if scanned > 0:
            lines.append(f"## Schema Drift")
            lines.append(f"- last {scanned} sessions clean (no drift)")
            lines.append("")

    lines.append(f"## Issue results")
    for r in summary["results"]:
        m = r.get("match") or {}
        lines.append(f"- **{r['id']}** — {r['title']}")
        lines.append(f"  - prior: `{r['prior_status']}`")
        lines.append(f"  - tier {m.get('tier','?')}: `{m.get('status','?')}`")
        if m.get("evidence"):
            lines.append(f"  - evidence: {m['evidence']}")
        if r["extra_tags"]:
            lines.append(f"  - extra tags: {r['extra_tags']}")
    if summary["suggestions"]:
        lines.append("")
        lines.append("## Feature → hook replacement suggestions")
        for s in summary["suggestions"]:
            lines.append(f"- release `{s['release']}` mentions `{s['keyword']}` — check if it replaces a workaround")
    body = "\n".join(lines) + "\n"
    if dry_run:
        print(body)
        return None
    with open(path, "w") as f:
        f.write(body)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Dashcam Env-Up")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    config = load_config()
    result = run_envup(offline=args.offline, dry_run=args.dry_run, config=config)
    drift = result.get("schema_drift") or {}
    print(json.dumps({
        "version": result["version"],
        "offline": result["offline"],
        "releases_scanned": result["releases_scanned"],
        "issues_total": result["issues_total"],
        "issues_resolved": result["issues_resolved"],
        "needs_user_confirm": result["needs_user_confirm"],
        "schema_drift_sessions": drift.get("sessions_with_drift", 0),
        "schema_drift_scanned": drift.get("scanned", 0),
        "report_path": result.get("report_path"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
