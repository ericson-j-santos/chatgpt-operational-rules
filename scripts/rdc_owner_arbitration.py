#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

INTERACTIVE_DIR = Path(r"C:\RemoteDesktopCommander")
LAUNCHER = INTERACTIVE_DIR / "start-remote-desktop-commander.cmd"
SUPERVISOR = INTERACTIVE_DIR / "rdc-interactive-supervisor.ps1"
HEADLESS_DIR = Path(r"C:\ProgramData\ReqSys\RdcSvc")
HEADLESS_RUNNER = HEADLESS_DIR / "rdc-headless-runner.cjs"
HEADLESS_CLAIM = HEADLESS_DIR / "rdc-headless-owner.json"
BACKUP_DIR = INTERACTIVE_DIR / "owner-arbitration-backups"

V3_MARKER = "REM RDC_LAUNCHER_V3_RESILIENT"
V4_MARKER = "REM RDC_LAUNCHER_V4_ARBITRATED"
V5_MARKER = "REM RDC_LAUNCHER_V5_READY_CLAIM"
PS1_MARKER = "# RDC_INTERACTIVE_OWNER_SUPERVISOR_V2"
HEADLESS_MARKER = "// RDC_HEADLESS_V3_READY_CLAIM"
PINNED_PACKAGE = "@wonderwhy-er/desktop-commander@0.2.51"

LAUNCHER_V5 = r'''@echo off
setlocal EnableExtensions
REM RDC_LAUNCHER_V5_READY_CLAIM
title Remote Desktop Commander - Governed Ready Claim
set "RDC_SUPERVISOR=C:\RemoteDesktopCommander\rdc-interactive-supervisor.ps1"
if not exist "%RDC_SUPERVISOR%" exit /b 31
if /I "%~1"=="--self-test" (
  powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%RDC_SUPERVISOR%" -SelfTest
  exit /b %ERRORLEVEL%
)
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%RDC_SUPERVISOR%"
exit /b %ERRORLEVEL%
'''.replace("\n", "\r\n")

SUPERVISOR_PS1 = r'''param([switch]$SelfTest)
# RDC_INTERACTIVE_OWNER_SUPERVISOR_V2
$ErrorActionPreference = "Stop"
$Package = "@wonderwhy-er/desktop-commander@0.2.51"
$ClaimPath = "C:\ProgramData\ReqSys\RdcSvc\rdc-headless-owner.json"
$ClaimMaxAgeSeconds = 8
$LogPath = "C:\RemoteDesktopCommander\rdc-interactive-owner.log"
$PollSeconds = 2
$RetrySeconds = 15

function Write-RdcLog([string]$Message) {
    Add-Content -LiteralPath $LogPath -Value ("{0:o} {1}" -f (Get-Date), $Message) -Encoding UTF8
}

function Get-HeadlessOwnerState {
    if (-not (Test-Path -LiteralPath $ClaimPath)) {
        return "inactive"
    }
    try {
        $raw = Get-Content -LiteralPath $ClaimPath -Raw -ErrorAction Stop
        $claim = $raw | ConvertFrom-Json -ErrorAction Stop
        if (-not $claim.ready -or -not $claim.updated_at) {
            return "inactive"
        }
        $updated = [DateTimeOffset]::Parse([string]$claim.updated_at).ToUniversalTime()
        $age = ([DateTimeOffset]::UtcNow - $updated).TotalSeconds
        if ($age -ge 0 -and $age -le $ClaimMaxAgeSeconds) {
            return "running"
        }
        return "inactive"
    } catch {
        return "error"
    }
}

function Stop-InteractiveTree([int]$ProcessId) {
    $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
    & $taskkill /PID $ProcessId /T /F *> $null
}

function Resolve-Npx {
    $cmd = Get-Command npx.cmd -ErrorAction Stop
    return $cmd.Source
}

if ($SelfTest) {
    $state = Get-HeadlessOwnerState
    if ($state -eq "error") {
        Write-RdcLog "self_test claim=error"
        exit 32
    }
    $npx = Resolve-Npx
    & $npx --yes $Package --version *> $null
    $code = $LASTEXITCODE
    Write-RdcLog ("self_test package={0} claim_state={1} code={2}" -f $Package,$state,$code)
    exit $code
}

while ($true) {
    $state = Get-HeadlessOwnerState
    if ($state -eq "running") {
        Write-RdcLog "standby owner=headless_ready_claim"
        Start-Sleep -Seconds $PollSeconds
        continue
    }
    if ($state -eq "error") {
        Write-RdcLog "fail_closed reason=headless_claim_error"
        Start-Sleep -Seconds $PollSeconds
        continue
    }

    try {
        $npx = Resolve-Npx
        $command = ('call "{0}" --yes {1} remote >> "{2}" 2>&1' -f $npx,$Package,$LogPath)
        $proc = Start-Process -FilePath $env:ComSpec -ArgumentList @("/d","/c",$command) -WindowStyle Hidden -PassThru
        Write-RdcLog ("interactive_child_start pid={0} claim_state={1}" -f $proc.Id,$state)

        while (-not $proc.HasExited) {
            Start-Sleep -Seconds $PollSeconds
            $owner = Get-HeadlessOwnerState
            if ($owner -eq "running" -or $owner -eq "error") {
                Write-RdcLog ("interactive_yield pid={0} reason={1}" -f $proc.Id,$owner)
                Stop-InteractiveTree -ProcessId $proc.Id
                try { $proc.WaitForExit() } catch {}
                break
            }
        }
        if ($proc.HasExited) {
            Write-RdcLog ("interactive_child_exit pid={0} code={1}" -f $proc.Id,$proc.ExitCode)
        }
    } catch {
        Write-RdcLog ("interactive_supervisor_error type={0}" -f $_.Exception.GetType().Name)
    }
    Start-Sleep -Seconds $RetrySeconds
}
'''

HEADLESS_RUNNER_V3 = r'''// RDC_HEADLESS_V3_READY_CLAIM
const fs = require('fs');
const { spawn } = require('child_process');

const logPath = "C:\\ProgramData\\ReqSys\\RdcSvc\\rdc-headless.log";
const claimPath = "C:\\ProgramData\\ReqSys\\RdcSvc\\rdc-headless-owner.json";
const claimTemp = claimPath + ".tmp";
const node = process.execPath;
const entry = "C:\\ProgramData\\ReqSys\\RdcSvc\\app\\node_modules\\@wonderwhy-er\\desktop-commander\\dist\\index.js";
const out = fs.openSync(logPath, 'a');
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function log(message) {
  fs.writeSync(out, new Date().toISOString() + ' ' + message + '\\n');
}

function clearClaim() {
  try {
    fs.unlinkSync(claimPath);
  } catch (err) {
    if (!err || err.code !== 'ENOENT') {
      log('claim_clear_error type=' + (err && err.name ? err.name : 'Error'));
    }
  }
  try {
    fs.unlinkSync(claimTemp);
  } catch (err) {
    if (err && err.code !== 'ENOENT') {
      log('claim_temp_clear_error type=' + (err.name || 'Error'));
    }
  }
}

function writeClaim(pid) {
  const payload = {
    schema: 'rdc-headless-owner-v1',
    ready: true,
    pid,
    updated_at: new Date().toISOString(),
  };
  fs.writeFileSync(claimTemp, JSON.stringify(payload) + '\\n', 'utf8');
  fs.renameSync(claimTemp, claimPath);
}

function runChild() {
  return new Promise((resolve) => {
    const child = spawn(node, [entry, 'remote'], {
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      env: process.env,
    });
    let ready = false;
    let heartbeat = null;
    let rolling = '';

    const consume = (chunk) => {
      try {
        fs.writeSync(out, chunk);
      } catch {}
      rolling = (rolling + chunk.toString('utf8')).slice(-16384);
      if (!ready && (
        rolling.includes('Device ready:') ||
        rolling.includes('Device marked as online')
      )) {
        ready = true;
        writeClaim(child.pid);
        log('owner_claim ready=true pid=' + child.pid);
        heartbeat = setInterval(() => {
          try {
            writeClaim(child.pid);
          } catch (err) {
            log('claim_heartbeat_error type=' + (err && err.name ? err.name : 'Error'));
          }
        }, 2000);
      }
    };

    child.stdout.on('data', consume);
    child.stderr.on('data', consume);
    child.on('error', (err) => {
      if (heartbeat) clearInterval(heartbeat);
      clearClaim();
      resolve({ code: 1, signal: 'spawn_error', error: err.name });
    });
    child.on('exit', (code, signal) => {
      if (heartbeat) clearInterval(heartbeat);
      clearClaim();
      resolve({ code: code ?? 1, signal: signal ?? 'none', ready });
    });
  });
}

async function main() {
  log('runner_start mode=primary_owner ready_claim=v1');
  clearClaim();
  while (true) {
    log('child_start');
    const result = await runChild();
    log('child_exit code=' + result.code + ' signal=' + result.signal + ' ready=' + result.ready);
    await delay(15000);
  }
}

process.on('exit', clearClaim);
process.on('SIGTERM', () => {
  clearClaim();
  process.exit(0);
});
process.on('SIGINT', () => {
  clearClaim();
  process.exit(0);
});

main().catch((err) => {
  clearClaim();
  log('runner_error type=' + (err && err.name ? err.name : 'Error'));
  process.exit(1);
});
'''

def sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

def inspect(
    launcher: Path = LAUNCHER,
    supervisor: Path = SUPERVISOR,
    headless: Path = HEADLESS_RUNNER,
) -> dict[str, object]:
    launcher_text = launcher.read_text(encoding="utf-8-sig", errors="replace") if launcher.is_file() else ""
    supervisor_text = supervisor.read_text(encoding="utf-8-sig", errors="replace") if supervisor.is_file() else ""
    headless_text = headless.read_text(encoding="utf-8-sig", errors="replace") if headless.is_file() else ""
    if V5_MARKER in launcher_text:
        launcher_marker = "v5"
    elif V4_MARKER in launcher_text:
        launcher_marker = "v4"
    elif V3_MARKER in launcher_text:
        launcher_marker = "v3"
    else:
        launcher_marker = "other"
    return {
        "launcher_exists": launcher.is_file(),
        "launcher_sha256": sha256(launcher),
        "launcher_marker": launcher_marker,
        "supervisor_exists": supervisor.is_file(),
        "supervisor_marker": PS1_MARKER in supervisor_text,
        "headless_exists": headless.is_file(),
        "headless_sha256": sha256(headless),
        "headless_marker": HEADLESS_MARKER in headless_text,
        "claim_path": str(HEADLESS_CLAIM),
    }

def _validate_sources(launcher: Path, headless: Path) -> None:
    if not launcher.is_file():
        raise FileNotFoundError(launcher)
    if not headless.is_file():
        raise FileNotFoundError(headless)
    launcher_text = launcher.read_text(encoding="utf-8-sig", errors="replace")
    headless_text = headless.read_text(encoding="utf-8-sig", errors="replace")
    if not any(marker in launcher_text for marker in (V3_MARKER, V4_MARKER, V5_MARKER)):
        raise ValueError("interactive launcher is not a governed V3/V4/V5 source")
    if HEADLESS_MARKER not in headless_text:
        required = ("spawn(", "desktop-commander", "'remote'")
        if not all(item in headless_text for item in required):
            raise ValueError("headless runner structure is not recognized")

def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8", newline="")
    os.replace(temp, path)

def apply(
    launcher: Path = LAUNCHER,
    supervisor: Path = SUPERVISOR,
    headless: Path = HEADLESS_RUNNER,
    backup_dir: Path = BACKUP_DIR,
) -> dict[str, object]:
    before = inspect(launcher, supervisor, headless)
    if before["launcher_marker"] == "v5" and before["supervisor_marker"] and before["headless_marker"]:
        return {"result": "already_applied", "before": before, "after": before, "backups": []}
    _validate_sources(launcher, headless)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups: list[tuple[Path, Path]] = []
    supervisor_existed = supervisor.exists()
    try:
        sources = [launcher, headless]
        if supervisor_existed:
            sources.append(supervisor)
        for source in sources:
            backup = backup_dir / f"{source.name}.{stamp}.bak"
            shutil.copy2(source, backup)
            backups.append((source, backup))
        _write_atomic(supervisor, SUPERVISOR_PS1)
        _write_atomic(launcher, LAUNCHER_V5)
        _write_atomic(headless, HEADLESS_RUNNER_V3)
        after = inspect(launcher, supervisor, headless)
        if (
            after["launcher_marker"] != "v5"
            or not after["supervisor_marker"]
            or not after["headless_marker"]
        ):
            raise RuntimeError("post-write ready-claim validation failed")
        return {
            "result": "READY_CLAIM_ARBITRATION_APPLIED",
            "before": before,
            "after": after,
            "backups": [str(item[1]) for item in backups],
        }
    except Exception:
        for source, backup in reversed(backups):
            if backup.is_file():
                shutil.copy2(backup, source)
        if not supervisor_existed:
            supervisor.unlink(missing_ok=True)
        raise

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install governed RDC readiness-claim owner arbitration"
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = apply() if args.apply else {"result": "dry_run", "state": inspect()}
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"result": "blocked", "error": str(exc), "error_type": type(exc).__name__},
                sort_keys=True,
            )
        )
        return 2

if __name__ == "__main__":
    raise SystemExit(main())