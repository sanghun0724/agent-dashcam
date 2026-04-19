#!/usr/bin/env node
// Agent Dashcam Stop hook — JSONL 파싱 → agent_dashcam_score.py → retention.py
// 모든 에러를 내부에서 포착하고 exit(0) 보장. stderr는 logs/hook-errors.log 에 append.
import { spawn } from "node:child_process";
import { appendFile, mkdir, readdir } from "node:fs/promises";
import { existsSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

const AGENT_DASHCAM_ROOT = process.env.AGENT_DASHCAM_ROOT || path.join(homedir(), ".claude", "agent-dashcam");
const SCRIPTS_DIR = path.join(AGENT_DASHCAM_ROOT, "scripts");
const LOG_PATH = path.join(AGENT_DASHCAM_ROOT, "logs", "hook-errors.log");
const HOOK_TIMEOUT_MS = 5000;

async function logError(tag, err) {
  try {
    await mkdir(path.dirname(LOG_PATH), { recursive: true });
    const ts = new Date().toISOString();
    let msg;
    if (err && err.stack) {
      msg = err.stack;
    } else if (err && typeof err === "object") {
      try { msg = JSON.stringify(err); } catch { msg = String(err); }
    } else {
      msg = String(err);
    }
    await appendFile(LOG_PATH, `${ts} | ${tag} | ${msg}\n`);
  } catch {
    // swallow — never let logging crash the hook
  }
}

function readStdin() {
  return new Promise((resolve) => {
    let data = "";
    if (process.stdin.isTTY) return resolve("");
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", () => resolve(data));
    setTimeout(() => resolve(data), 2000);
  });
}

function cwdToProjectDir(cwd) {
  if (!cwd) return null;
  const trimmed = cwd.replace(/^\//, "").replace(/\/+$/, "");
  return "-" + trimmed.replace(/\//g, "-");
}

async function pathExists(p) {
  try {
    return existsSync(p);
  } catch {
    return false;
  }
}

async function resolveJsonlPath(sessionId, cwd) {
  if (!sessionId) return null;
  const projects = path.join(homedir(), ".claude", "projects");
  if (cwd) {
    const primary = path.join(projects, cwdToProjectDir(cwd), `${sessionId}.jsonl`);
    if (await pathExists(primary)) return primary;
    try {
      const resolved = realpathSync(cwd);
      const alt = path.join(projects, cwdToProjectDir(resolved), `${sessionId}.jsonl`);
      if (await pathExists(alt)) return alt;
    } catch {
      // ignore — fall through to glob
    }
  }
  try {
    const dirs = await readdir(projects, { withFileTypes: true });
    for (const d of dirs) {
      if (!d.isDirectory()) continue;
      const candidate = path.join(projects, d.name, `${sessionId}.jsonl`);
      if (await pathExists(candidate)) return candidate;
    }
  } catch (e) {
    await logError("resolve_scan", e);
  }
  return null;
}

function runPython(scriptName, args, timeoutMs) {
  return new Promise((resolve) => {
    const proc = spawn("python3", [path.join(SCRIPTS_DIR, scriptName), ...args], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      try { proc.kill("SIGTERM"); } catch {}
      resolve({ code: 124, stdout, stderr: stderr + "\n[TIMEOUT]" });
    }, timeoutMs);
    proc.stdout.on("data", (d) => { stdout += d.toString(); });
    proc.stderr.on("data", (d) => { stderr += d.toString(); });
    proc.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? 0, stdout, stderr });
    });
    proc.on("error", (err) => {
      clearTimeout(timer);
      resolve({ code: 1, stdout, stderr: String(err) });
    });
  });
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
  const sessionId = payload.sessionId || payload.session_id;
  const cwd = payload.cwd;

  let jsonlPath;
  try {
    jsonlPath = await resolveJsonlPath(sessionId, cwd);
  } catch (e) {
    await logError("resolve_jsonl", e);
  }

  if (!jsonlPath) {
    await logError("JSONL_NOT_FOUND", { sessionId, cwd });
    return;
  }

  try {
    const scoreResult = await runPython("agent_dashcam_score.py", ["--input", jsonlPath, "--save"], HOOK_TIMEOUT_MS);
    if (scoreResult.code !== 0) {
      await logError("agent_dashcam_score_fail", { code: scoreResult.code, stderr: scoreResult.stderr.slice(0, 500) });
    }
  } catch (e) {
    await logError("agent_dashcam_score_throw", e);
  }

  try {
    const retResult = await runPython("retention.py", [], 3000);
    if (retResult.code !== 0) {
      await logError("retention_fail", { code: retResult.code, stderr: retResult.stderr.slice(0, 500) });
    }
  } catch (e) {
    await logError("retention_throw", e);
  }
}

main()
  .catch(async (e) => {
    await logError("top_level", e);
  })
  .finally(() => {
    process.exit(0);
  });
