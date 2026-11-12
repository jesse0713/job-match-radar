# Register Windows scheduled tasks for the daily job-match push.
#
# NOTE: this file is intentionally ASCII-only. PowerShell 5.1 decodes BOM-less
# scripts as GBK, so a non-ASCII *literal* inside the script gets mangled.
# We therefore avoid hard-coded non-ASCII paths and let $PSScriptRoot provide
# the project directory. If the project path itself contains non-ASCII
# characters and Task Scheduler still complains, create an ASCII junction and
# pass it with -Root:
#   cmd /c mklink /J "%USERPROFILE%\JobMatch" "<project dir>"
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Root "%USERPROFILE%\JobMatch"
#
# IMPORTANT: the task must point at an interpreter whose packages survive
# upgrades. Some desktop apps re-provision their bundled Python with a fixed
# package list and wipe anything pip-installed into it, so DrissionPage
# disappears and every run dies at `import collect` with no console, no log
# and no mail. This script only accepts an interpreter that can actually
# `import DrissionPage`, and prints the probe result for every candidate.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Python "C:\Path\pythonw.exe"
param(
    [string]$Python = "",
    [string]$Root   = "",
    [string]$TaskPrefix = "JobMatch",
    [string]$WatchdogAt = ""
)
$ErrorActionPreference = "Stop"

if (-not $Root) { $Root = $PSScriptRoot }
$Root = (Resolve-Path -LiteralPath $Root).Path

if (-not (Test-Path (Join-Path $Root "run_daily.py"))) {
    throw "Root does not look like the project dir: $Root"
}
$script   = Join-Path $Root "run_daily.py"
$dog      = Join-Path $Root "watchdog.py"

function Test-CollectorDeps([string]$exe) {
    # pythonw.exe is a GUI subsystem binary: it never prints to our console.
    # Probe with the matching python.exe so we can see the failure reason.
    $probe = $exe
    if ($probe -match 'pythonw\.exe$') { $probe = $probe -replace 'pythonw\.exe$', 'python.exe' }
    if (-not (Test-Path $probe)) { return "no-python-exe" }
    # A probe that is *supposed* to fail writes to stderr; with EAP=Stop that
    # would abort the script, so downgrade it just for this call.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $err = & $probe -c "import DrissionPage" 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -eq 0) { return "ok" }
    $tail = ($err | Select-Object -Last 1)
    return "missing-DrissionPage ($tail)"
}

$candidates = @()
if ($Python) { $candidates += $Python }
# 1) pythonw on PATH, 2) whatever the py launcher resolves, 3) common per-user installs.
$onPath = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $onPath) { $onPath = Get-Command python.exe -ErrorAction SilentlyContinue }
if ($onPath) { $candidates += $onPath.Source }

$pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($pyLauncher) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $exe = & $pyLauncher.Source -3 -c "import sys; print(sys.executable)" 2>$null } finally {
        $ErrorActionPreference = $prev
    }
    if ($exe) { $candidates += ($exe.Trim() -replace 'python\.exe$', 'pythonw.exe') }
}

$localPython = Join-Path $env:LOCALAPPDATA "Programs\Python"
if (Test-Path $localPython) {
    $candidates += @(Get-ChildItem -Path $localPython -Filter "pythonw.exe" -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
}
$candidates = @($candidates | Where-Object { $_ } | Select-Object -Unique)

$chosen = ""
$probes = @()
foreach ($cand in $candidates) {
    if (-not (Test-Path $cand)) { $probes += "  [skip] $cand (not found)"; continue }
    $state = Test-CollectorDeps $cand
    $probes += "  [$state] $cand"
    if ($state -eq "ok" -and -not $chosen) { $chosen = $cand }
}
Write-Output "--- interpreter probe ---"
$probes | ForEach-Object { Write-Output $_ }

if (-not $chosen) {
    throw ("No Python with DrissionPage found. Install the deps first:`n" +
           "  python -m pip install -r " + (Join-Path $Root "requirements.txt") + "`n" +
           "then re-run this script (or pass -Python <pythonw.exe>).")
}
Write-Output "using interpreter: $chosen"

# run_times comes from config.json so the schedule and the code stay in sync
$cfgPath = Join-Path $Root "config.json"
$times = @("11:30", "18:00")
if (Test-Path $cfgPath) {
    try {
        $cfg = [System.IO.File]::ReadAllText($cfgPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        if ($cfg.search.run_times) { $times = @($cfg.search.run_times) }
        if (-not $WatchdogAt -and $cfg.search.watchdog_time) { $WatchdogAt = [string]$cfg.search.watchdog_time }
    } catch {
        Write-Warning "config.json parse failed, using default times"
    }
}
if (-not $WatchdogAt) { $WatchdogAt = "13:30" }

# Weekdays only: no weekend pushes. The code also refuses weekends, because
# StartWhenAvailable can replay a missed Friday run on Saturday morning.
$weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -WakeToRun `
                -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit (New-TimeSpan -Minutes 25) -MultipleInstances IgnoreNew

$wanted = @()
foreach ($slot in $times) {
    $name    = "$TaskPrefix-" + ($slot -replace ":", "")
    $action  = New-ScheduledTaskAction -Execute $chosen -Argument "`"$script`" --slot $slot" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $slot
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
        -Description "Weekday job match push at $slot, Mon-Fri (interpreter: $chosen)" -Force | Out-Null
    Write-Output "registered ${name}: Mon-Fri at $slot"
    $wanted += $name
}

# Watchdog: run_daily itself crashes silently if its interpreter is broken.
# This one only needs stdlib + mailer, and mails you when today's push produced
# no successful send. It runs after the push slot, still weekdays only.
if (Test-Path $dog) {
    $name    = "$TaskPrefix-Watchdog"
    $action  = New-ScheduledTaskAction -Execute $chosen -Argument "`"$dog`"" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $WatchdogAt
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
        -Description "Alerts when today's job push produced no successful send (Mon-Fri)" -Force | Out-Null
    Write-Output "registered ${name}: Mon-Fri at $WatchdogAt"
    $wanted += $name
}

# Second profile: legal / compliance search (profiles/legal/config.json).
# It must not overlap the base slot: both runs share the same Chrome on port 9333.
$legalCfgPath = Join-Path $Root "profiles\legal\config.json"
if (Test-Path $legalCfgPath) {
    $legalTimes = @("12:45")
    $legalDogAt = "13:45"
    try {
        $legalCfg = [System.IO.File]::ReadAllText($legalCfgPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        if ($legalCfg.search.run_times) { $legalTimes = @($legalCfg.search.run_times) }
        if ($legalCfg.search.watchdog_time) { $legalDogAt = [string]$legalCfg.search.watchdog_time }
    } catch {
        Write-Warning "profiles/legal/config.json parse failed, using default times"
    }
    foreach ($slot in $legalTimes) {
        $name    = "$TaskPrefix-Legal-" + ($slot -replace ":", "")
        $action  = New-ScheduledTaskAction -Execute $chosen -Argument "`"$script`" --slot $slot --profile legal" -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $slot
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
            -Description "Weekday legal/compliance job push at $slot, Mon-Fri (profile=legal)" -Force | Out-Null
        Write-Output "registered ${name}: Mon-Fri at $slot (profile=legal)"
        $wanted += $name
    }
    $name    = "$TaskPrefix-Legal-Watchdog"
    $action  = New-ScheduledTaskAction -Execute $chosen -Argument "`"$dog`" --profile legal" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $legalDogAt
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
        -Description "Alerts when today's legal profile push produced no successful send (Mon-Fri)" -Force | Out-Null
    Write-Output "registered ${name}: Mon-Fri at $legalDogAt (profile=legal)"
    $wanted += $name
}

# Remove stale tasks not present in run_times (e.g. old 1130 / 1800 after a time change)
Get-ScheduledTask -TaskName "$TaskPrefix-*" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($wanted -notcontains $_.TaskName) {
        Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
        Write-Output "removed stale task $($_.TaskName)"
    }
}

Write-Output "--- current state ---"
Get-ScheduledTask -TaskName "$TaskPrefix-*" | ForEach-Object {
    $a = $_.Actions[0]
    $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
    "{0}: `"{1}`" {2} | next={3} | lastRun={4} | lastResult={5}" -f $_.TaskName, $a.Execute, $a.Arguments, $info.NextRunTime, $info.LastRunTime, $info.LastTaskResult
}
