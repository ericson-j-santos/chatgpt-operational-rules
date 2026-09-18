param(
  [string]$TaskName = 'TodoGlobal24x7NoteriHA',
  [string]$Account = "$env:COMPUTERNAME\$env:USERNAME",
  [Parameter(Mandatory=$true)][string]$Python,
  [Parameter(Mandatory=$true)][string]$RepoRoot
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path $Python)) { throw "Python não encontrado: $Python" }
if (-not (Test-Path $RepoRoot)) { throw "RepoRoot não encontrado: $RepoRoot" }
$worker = Join-Path $RepoRoot 'scripts\deploy_pc24x7_noteri_ha_worker_dev.py'
if (-not (Test-Path $worker)) { throw "Worker não encontrado: $worker" }
$arguments = '-m scripts.deploy_pc24x7_noteri_ha_worker_dev'
$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
