# 本机定时兜底：用 Windows 计划任务每天 09:10 / 20:10 跑一次签到
# 优先 pythonw.exe（无黑框窗口），并开启 StartWhenAvailable 以便错过后补跑。
# 重复执行只会覆盖同名任务，不会产生重复条目。

param(
    [string]$TaskName = "WorkBuddyCheckin"
)

$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Script = Join-Path $Dir "checkin.py"

if (-not (Test-Path $Script)) {
    Write-Error "找不到 checkin.py: $Script"
    exit 1
}

# 1) 选择解释器：优先 pythonw.exe（不弹窗）
$PythonW = $null
foreach ($cmd in @("pythonw.exe", "pythonw3.exe")) {
    $p = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($p) { $PythonW = $p.Source; break }
}
if (-not $PythonW) {
    $py = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($py) {
        $candidate = Join-Path (Split-Path $py.Source) "pythonw.exe"
        if (Test-Path $candidate) { $PythonW = $candidate } else { $PythonW = $py.Source }
    }
}
if (-not $PythonW) {
    Write-Error "未找到 Python，请先安装 Python 3.9+ 并加入 PATH"
    exit 1
}

Write-Host "[info] 解释器: $PythonW"

# 2) 动作：一个任务挂两个动作，依次跑两个账号（计划任务不支持给动作设环境变量，
#    所以用 --account 指定，避免在任务里写中文环境变量）
$Actions = @(
    (New-ScheduledTaskAction -Execute $PythonW -Argument "`"$Script`" --account 1" -WorkingDirectory $Dir),
    (New-ScheduledTaskAction -Execute $PythonW -Argument "`"$Script`" --account 2" -WorkingDirectory $Dir)
)

# 3) 两个每日触发器（本地时间，脚本内按北京时间记录日志）
$Triggers = @(
    (New-ScheduledTaskTrigger -Daily -At "09:10"),
    (New-ScheduledTaskTrigger -Daily -At "20:10")
)

# 4) 设置：错过时间点后补跑、允许电池供电运行
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# 5) 覆盖注册（同名任务先删除，天然去重）
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "[info] 已存在同名任务，先移除再重建"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $Actions -Trigger $Triggers `
    -Settings $Settings `
    -Description "WorkBuddy daily check-in x2 accounts (09:10 / 20:10)" | Out-Null

Write-Host "[ok] 计划任务已注册: $TaskName"
Get-ScheduledTask -TaskName $TaskName |
    Select-Object TaskName, State |
    Format-Table -AutoSize

Write-Host ""
Write-Host "立即测试一次:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "查看结果:      Get-Content `"$Dir\state\checkin.log`" -Tail 5"
