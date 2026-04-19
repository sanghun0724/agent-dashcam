#!/usr/bin/env node
// Agent Dashcam SessionStart hook — 최근 3세션 점수표 브리핑
// stdout: hookSpecificOutput.additionalContext 로 system-reminder 주입
// 모든 에러는 내부에서 포착 → logs/hook-errors.log append → 항상 exit(0)
import { appendFile, mkdir, readdir, readFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";

const AGENT_DASHCAM_ROOT = process.env.AGENT_DASHCAM_ROOT || path.join(homedir(), ".claude", "agent-dashcam");
const SCORES_DIR = path.join(AGENT_DASHCAM_ROOT, "scores");
const LOG_PATH = path.join(AGENT_DASHCAM_ROOT, "logs", "hook-errors.log");
const RECENT_N = 3;

async function logError(tag, err) {
  try {
    await mkdir(path.dirname(LOG_PATH), { recursive: true });
    const ts = new Date().toISOString();
    let msg;
    if (err && err.stack) msg = err.stack;
    else if (err && typeof err === "object") {
      try { msg = JSON.stringify(err); } catch { msg = String(err); }
    } else msg = String(err);
    await appendFile(LOG_PATH, `${ts} | ${tag} | ${msg}\n`);
  } catch {
    // swallow
  }
}

function readStdin() {
  return new Promise((resolve) => {
    let data = "";
    if (process.stdin.isTTY) return resolve("");
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (c) => { data += c; });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", () => resolve(data));
    setTimeout(() => resolve(data), 1000);
  });
}

function cwdToProjectDir(cwd) {
  if (!cwd) return null;
  const trimmed = cwd.replace(/^\//, "").replace(/\/+$/, "");
  return "-" + trimmed.replace(/\//g, "-");
}

async function loadRecentScores(projectKey) {
  let entries;
  try {
    entries = await readdir(SCORES_DIR, { withFileTypes: true });
  } catch {
    return [];
  }
  const prefix = projectKey ? `${projectKey}__` : "";
  const candidates = entries
    .filter((e) => e.isFile() && e.name.endsWith(".json") && (!prefix || e.name.startsWith(prefix)))
    .map((e) => path.join(SCORES_DIR, e.name));
  const withStats = [];
  for (const p of candidates) {
    try {
      const raw = await readFile(p, "utf8");
      const json = JSON.parse(raw);
      const scoredAt = json?.meta?.scored_at;
      const ts = scoredAt ? Date.parse(scoredAt) : 0;
      withStats.push({ path: p, data: json, ts });
    } catch (e) {
      await logError("score_parse_skip", { file: p, error: String(e) });
    }
  }
  withStats.sort((a, b) => b.ts - a.ts);
  return withStats.slice(0, RECENT_N);
}

function pickLowestAxes(axes, n = 2) {
  const entries = Object.entries(axes || {}).filter(([, v]) => typeof v === "number");
  entries.sort((a, b) => a[1] - b[1]);
  return entries.slice(0, n).map(([name, score]) => ({ name, score }));
}

// 축별 원라이너 액션 tip (daily_report.py _AXIS_SUGGESTIONS 축약)
const AXIS_TIPS = {
  context_efficiency: "Grep → 필요 부분만 Read, 탐색은 Explore agent 위임",
  cost_efficiency: "단순 작업 Haiku, 표준 Sonnet. 독립 tool call 병렬 실행",
  cost_per_useful_output: "커밋 빈도↑, 테스트/PR 한 세션 내 마무리",
  role_focus: "executor/explore/architect 위임 확대, skill 키워드 활용",
  read_edit_ratio: "Edit 전 관련 함수 Read 1~3개 습관화 (이상적 2~6)",
  reasoning_loop: "/plan 또는 /ralplan 으로 계획 세운 후 실행",
  sentiment: "가정 먼저 말하고 AskUserQuestion 으로 확인받기",
  constraint_adherence: "--no-verify 금지, 위반 로그 logs/hook-errors.log 확인",
  hook_health: "hook-errors.log 확인, 문제 훅은 OMC_SKIP_HOOKS 로 스킵",
  operational_bottleneck: "독립 작업 병렬 실행, 긴 빌드는 run_in_background",
};

function actionTip(axisName) {
  return AXIS_TIPS[axisName] ?? "axis 검토 필요";
}

function computeTrend(sessions) {
  const avgs = sessions.map((s) => s.data.weighted_avg).filter((v) => typeof v === "number");
  if (avgs.length < 2) return { direction: "n/a", delta: 0, samples: avgs.length };
  const latest = avgs[0];
  const prior = avgs[avgs.length - 1];
  const delta = Number((latest - prior).toFixed(4));
  const direction = delta > 0.02 ? "up" : delta < -0.02 ? "down" : "flat";
  return { direction, delta, samples: avgs.length };
}

function formatBriefing(sessions) {
  if (!sessions.length) return null;
  const latest = sessions[0].data;
  const meta = latest?.meta ?? {};
  const suppressed = Array.isArray(meta.suppressed_axes) ? meta.suppressed_axes : [];
  // 억제 축은 tip 대상에서 제외 (세션 타입상 자연히 낮음)
  const lowestAll = pickLowestAxes(latest.axes, 4);
  const lowestActive = lowestAll.filter((a) => !suppressed.includes(a.name)).slice(0, 2);
  const displayLowest = lowestActive.length ? lowestActive : lowestAll.slice(0, 2);
  const trend = computeTrend(sessions);
  const lowStr = displayLowest.map((a) => `${a.name}=${a.score}`).join(", ");
  const trendStr = trend.samples < 2
    ? `(first session on record)`
    : `trend=${trend.direction} (Δ${trend.delta >= 0 ? "+" : ""}${trend.delta} over ${trend.samples} sessions)`;
  const typeStr = meta.session_type
    ? `type=${meta.session_type}${typeof meta.session_type_confidence === "number" ? ` (conf ${meta.session_type_confidence})` : ""}`
    : null;
  const lines = [
    `[Agent Dashcam briefing] last session weighted_avg=${latest.weighted_avg}`,
    `  lowest axes: ${lowStr}${suppressed.length ? ` (suppressed: ${suppressed.join(", ")})` : ""}`,
    `  ${trendStr}${typeStr ? `, ${typeStr}` : ""}`,
    `  sessionId=${meta.sessionId ?? "unknown"}, scored_at=${meta.scored_at ?? "?"}`,
  ];
  // 가장 약한 축(억제 제외)에 대한 액션 tip
  const weakest = displayLowest[0];
  if (weakest && typeof weakest.score === "number" && weakest.score < 0.5 && !suppressed.includes(weakest.name)) {
    lines.push(`  🩺 tip for \`${weakest.name}\`: ${actionTip(weakest.name)}`);
  }
  return lines.join("\n");
}

async function main() {
  let payload = {};
  try {
    const raw = await readStdin();
    if (raw.trim()) payload = JSON.parse(raw);
  } catch (e) {
    await logError("stdin_parse", e);
  }
  const cwd = payload.cwd;
  const projectKey = cwdToProjectDir(cwd);

  let sessions = [];
  try {
    sessions = await loadRecentScores(projectKey);
  } catch (e) {
    await logError("load_scores", e);
  }

  const briefing = formatBriefing(sessions);
  if (!briefing) {
    return;
  }

  const output = {
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      additionalContext: briefing,
    },
  };
  process.stdout.write(JSON.stringify(output));
}

main()
  .catch(async (e) => { await logError("top_level", e); })
  .finally(() => { process.exit(0); });
