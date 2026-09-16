param(
  [string]$TaskName = 'TodoGlobal24x7Headless',
  [string]$Account = "$env:COMPUTERNAME\$env:USERNAME",
  [string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
  [string]$RuntimeDir = "$env:LOCALAPPDATA\ReqSys\TodoGlobal24x7",
  [Parameter(Mandatory=$true)][string]$RepoRoot,
  [string]$BeaconHost = '',
  [int]$BeaconPort = 19194
)
$ErrorActionPreference = 'Stop'
$script = Join-Path $RuntimeDir 'headless_boot_runner.py'
if (-not (Test-Path $Python)) { throw "Python não encontrado: $Python" }
if (-not (Test-Path $script)) { throw "Runner não encontrado: $script" }
if (-not (Test-Path $RepoRoot)) { throw "RepoRoot não encontrado: $RepoRoot" }
$arguments = '"' + $script + '" --runtime-dir "' + $RuntimeDir + '" --repo-root "' + $RepoRoot + '"'
if ($BeaconHost) { $arguments += ' --beacon-host ' + $BeaconHost + ' --beacon-port ' + $BeaconPort }
$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
