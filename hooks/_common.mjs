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
// Cap stdin at 1 MiB. Real Codex/Gemini Stop payloads are < 1 KiB; anything
// larger is either a bug or a DoS attempt. The hook exits 0 either way.
export const MAX_STDIN_BYTES = 1 * 1024 * 1024;

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
    let truncated = false;
    if (process.stdin.isTTY) return resolve("");
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      if (truncated) return;
      if (data.length + chunk.length > MAX_STDIN_BYTES) {
        truncated = true;
        data += chunk.slice(0, Math.max(0, MAX_STDIN_BYTES - data.length));
        try { process.stdin.destroy(); } catch {}
        resolve(data);
        return;
      }
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", () => resolve(data));
    setTimeout(() => resolve(data), 2000);
  });
}

export async function pathExists(p) {
  try { return existsSync(p); } catch { return false; }
}

// Resolve `p` (symlinks not followed — we stop at path.resolve) and return true
// only if it lives under one of the allowed roots. Prevents a malicious
// transcript_path on stdin from routing the scorer to arbitrary filesystem
// locations. Roots are resolved once and compared as path prefixes.
// `AGENT_DASHCAM_HOOK_EXTRA_ROOTS` env var (path.delimiter-separated) extends
// the default allowlist — used by tests to whitelist tempdirs, and by users
// who keep sessions outside the three default provider dirs.
export function isAllowedPath(p, extraRoots = []) {
  if (!p) return false;
  const resolved = path.resolve(p);
  const envExtra = (process.env.AGENT_DASHCAM_HOOK_EXTRA_ROOTS || "")
    .split(path.delimiter).filter(Boolean);
  const roots = [
    path.join(homedir(), ".codex"),
    path.join(homedir(), ".gemini"),
    path.join(homedir(), ".claude"),
    AGENT_DASHCAM_ROOT,
    ...extraRoots,
    ...envExtra,
  ].map((r) => path.resolve(r));
  return roots.some((root) => resolved === root || resolved.startsWith(root + path.sep));
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
