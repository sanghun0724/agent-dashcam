// Shared helpers for Agent Dashcam stop-hook wrappers.
// Used by hooks/codex-stop.mjs and hooks/gemini-stop.mjs.
import { spawn } from "node:child_process";
import { appendFile, mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

export const AGENT_DASHCAM_ROOT = process.env.AGENT_DASHCAM_ROOT || path.join(homedir(), ".claude", "agent-dashcam");
export const SCRIPTS_DIR = path.join(AGENT_DASHCAM_ROOT, "scripts");
export const LOG_PATH = path.join(AGENT_DASHCAM_ROOT, "logs", "hook-errors.log");
export const HOOK_TIMEOUT_MS = 5000;

export function makeLogger(hookName) {
  return async function logError(tag, err) {
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
      await appendFile(LOG_PATH, `${ts} | ${hookName} | ${tag} | ${msg}\n`);
    } catch {
      // swallow — never let logging crash the hook
    }
  };
}

export function readStdin() {
  return new Promise((resolve) => {
    let data = "";
    if (process.stdin.isTTY) return resolve("");
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { data += chunk; });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", () => resolve(data));
    setTimeout(() => resolve(data), 2000);
  });
}

export async function pathExists(p) {
  try { return existsSync(p); } catch { return false; }
}

export function runPython(scriptName, args, timeoutMs = HOOK_TIMEOUT_MS) {
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
