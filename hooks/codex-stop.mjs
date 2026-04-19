#!/usr/bin/env node
// Agent Dashcam Stop hook for Codex CLI.
// Wire into ~/.codex/hooks.json:
//   {"hooks": {"Stop": [{"command": "node $HOME/.claude/agent-dashcam/hooks/codex-stop.mjs"}]}}
// stdin payload (Codex): {"session_id": "...", "rollout_path": "..."} (or "transcript_path").
// All errors captured internally; exit(0) always.
import { readdir, stat } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";
import {
  makeLogger, readStdin, pathExists, runPython,
} from "./_common.mjs";

const logError = makeLogger("codex-stop");
const DRY_RUN = process.argv.includes("--dry-run");

async function findRecentRollout(sessionId) {
  // Fallback search when only sessionId is supplied.
  // Codex stores sessions under ~/.codex/sessions/YYYY/MM/DD/rollout-*-<sessionId>.jsonl
  const root = path.join(homedir(), ".codex", "sessions");
  if (!(await pathExists(root))) return null;
  async function* walk(dir) {
    let entries;
    try { entries = await readdir(dir, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      const full = path.join(dir, e.name);
      if (e.isDirectory()) yield* walk(full);
      else if (e.isFile() && e.name.endsWith(".jsonl") && e.name.includes(sessionId)) yield full;
    }
  }
  let newest = null;
  let newestMtime = 0;
  for await (const p of walk(root)) {
    try {
      const s = await stat(p);
      if (s.mtimeMs > newestMtime) { newest = p; newestMtime = s.mtimeMs; }
    } catch {
      // ignore
    }
  }
  return newest;
}

async function resolveRollout(payload) {
  const direct = payload.transcript_path || payload.rollout_path || payload.path;
  if (direct && await pathExists(direct)) return direct;
  const sid = payload.session_id || payload.sessionId;
  if (sid) {
    const found = await findRecentRollout(sid);
    if (found) return found;
  }
  return null;
}

async function main() {
  let stdinRaw = "";
  let payload = {};
  try {
    stdinRaw = await readStdin();
    if (stdinRaw.trim()) payload = JSON.parse(stdinRaw);
  } catch (e) {
    await logError("stdin_parse", e);
  }

  let rolloutPath;
  try {
    rolloutPath = await resolveRollout(payload);
  } catch (e) {
    await logError("resolve_rollout", e);
  }

  if (!rolloutPath) {
    await logError("ROLLOUT_NOT_FOUND", { payload });
    return;
  }

  if (DRY_RUN) {
    process.stdout.write(JSON.stringify({ hook: "codex-stop", dry_run: true, rollout: rolloutPath }) + "\n");
    return;
  }

  try {
    const r = await runPython("agent_dashcam_score.py",
      ["--input", rolloutPath, "--provider", "codex", "--save"]);
    if (r.code !== 0) {
      await logError("agent_dashcam_score_fail", { code: r.code, stderr: r.stderr.slice(0, 500) });
    }
  } catch (e) {
    await logError("agent_dashcam_score_throw", e);
  }
}

main()
  .catch(async (e) => { await logError("top_level", e); })
  .finally(() => { process.exit(0); });
