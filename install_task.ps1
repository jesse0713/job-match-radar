# 注册 Windows 计划任务：按 config.json 里的 run_times 自动采集并邮件推送
# 以当前用户身份运行，无需管理员权限。
# 删除任务：Unregister-ScheduledTask -TaskName "JobMatch-1130" -Confirm:$false
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Python "C:\Path\To\pythonw.exe"
param(
    [string]$Python = "",
    [string]$TaskPrefix = "JobMatch"
)
$ErrorActionPreference = "Stop"

$Root   = $PSScriptRoot
$script = Join-Path $Root "run_daily.py"
$work   = $Root

if (-not $Python) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command python.exe -ErrorAction SilentlyContinue }
    if (-not $cmd) { throw "未找到 Python，请用 -Python 指定 pythonw.exe 的完整路径" }
    $Python = $cmd.Source
}
if (-not (Test-Path $Python)) { throw "Python 路径不存在：$Python" }

$cfg   = Get-Content (Join-Path $Root "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$times = @($cfg.search.run_times)
if (-not $times -or $times.Count -eq 0) { $times = @("11:30") }

foreach ($slot in $times) {
    $name    = "$TaskPrefix-" + ($slot -replace ":", "")
    $action  = New-ScheduledTaskAction -Execute $Python -Argument "`"$script`" --slot $slot" -WorkingDirectory $work
    $trigger = New-ScheduledTaskTrigger -Daily -At $slot
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
                    -ExecutionTimeLimit (New-TimeSpan -Minutes 25) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
        -Description "岗位匹配自动推送 $slot" -Force | Out-Null
    Write-Output "OK  $name  ->  $slot"
}

Get-ScheduledTask -TaskName "$TaskPrefix-*" | Select-Object TaskName, State | Format-Table -AutoSize
