param(
    [switch]$Install,
    [switch]$Remove,
    [string]$TaskName = "ChinaStockEngine-Wind-Local",
    [string]$ConfigPath = ".private-stock-data\wind\local_daily.json"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $Root
if ($Install -and $Remove) { throw "Choose Install or Remove." }
if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    return
}
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (!(Test-Path -LiteralPath $Python)) { throw "Run setup-wind-windows.ps1 first." }
$Config = (Resolve-Path -LiteralPath $ConfigPath).Path
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if (!$Install) {
    Write-Host "Preview only. Use -Install after Windows canary and local config review."
    Write-Host "User: $User (interactive session only); config: $Config"
    Write-Host "At logon and every 5 minutes. No native calls without fresh local readiness."
    return
}
$Action = New-ScheduledTaskAction -Execute $Python `
    -Argument ('-m china_stock_engine.wind_daily tick --config "{0}"' -f $Config) `
    -WorkingDirectory $Root
$Logon = New-ScheduledTaskTrigger -AtLogOn -User $User
# Once + repetition, with no duration, repeats indefinitely. StartWhenAvailable
# covers missed triggers after sleep/boot; tick decides real completed sessions.
$Repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Task = New-ScheduledTask -Action $Action -Trigger @($Logon, $Repeat) `
    -Principal $Principal -Settings $Settings -Description "Local Wind daily/catch-up; remote triggers remain idle."
# Deliberately do not overwrite an existing task or start any Wind session here.
Register-ScheduledTask -TaskName $TaskName -InputObject $Task | Out-Null
Write-Host "Registered $TaskName. It waits for local readiness; no SDK login was attempted."
