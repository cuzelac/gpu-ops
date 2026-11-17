<#
.SYNOPSIS
  Install, inspect or remove the "gpu-ops reaper" Windows scheduled task.

.DESCRIPTION
    show       Print exactly what `install` would register. Changes nothing. (default)
    install    Register the task: every 30 minutes while you're logged in, run gpuops-reaper.ps1 hidden.
    status     Show whether it is installed, and its last and next run.
    uninstall  Remove the task.

  The task runs as you, only while you are logged on ("Interactive" logon). That lets it read the
  1Password service account token from Windows Credential Manager without Task Scheduler storing your
  Windows password. While you're logged off it does not run; the on-machine dead-man timer covers that.
  No administrator rights are needed.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\Users\cuzel\src\gpu-ops\bin\gpuops-reaper-task.ps1 show
#>
param(
    [Parameter(Position = 0)][ValidateSet('show', 'install', 'status', 'uninstall')][string]$Action = 'show',
    [int]$Minutes = 30
)
$ErrorActionPreference = 'Stop'

$TaskName = 'gpu-ops reaper'
$TaskPath = '\gpu-ops\'
$script = Join-Path $PSScriptRoot 'gpuops-reaper.ps1'
# conhost --headless runs the console program with no window, so nothing flashes on screen every 30 minutes
$exe = Join-Path $env:WINDIR 'System32\conhost.exe'
$arg = "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`""
$user = "$env:USERDOMAIN\$env:USERNAME"
$desc = "Deletes gpu-ops cloud GPU machines more than 10 minutes past their deadline. Installed from $PSScriptRoot. Remove with gpuops-reaper-task.ps1 uninstall."

switch ($Action) {
    'show' {
        Write-Host "Task       : $TaskPath$TaskName"
        Write-Host "Runs as    : $user, only while logged on (no stored password, no admin)"
        Write-Host "Schedule   : every $Minutes minutes, starting 1 minute after install, indefinitely"
        Write-Host "Command    : $exe $arg"
        Write-Host "Limits     : one instance at a time; killed after 10 minutes; runs on battery; catches up after sleep"
        Write-Host "Log        : $(Join-Path $env:LOCALAPPDATA 'gpu-ops\reaper.log')"
    }
    'install' {
        if (-not (Test-Path $script)) { throw "missing $script" }
        # not $action: PowerShell variables are case-insensitive, and $Action is this script's validated parameter
        $taskAction = New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $PSScriptRoot
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
            -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $taskAction -Trigger $trigger -Settings $settings `
            -Principal $principal -Description $desc -Force | Out-Null
        Write-Host "Installed $TaskPath$TaskName (every $Minutes min). Check with: gpuops-reaper-task.ps1 status"
    }
    'status' {
        $t = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
        if (-not $t) { Write-Host 'not installed'; exit 1 }
        $i = $t | Get-ScheduledTaskInfo
        Write-Host "State      : $($t.State)"
        Write-Host "Last run   : $($i.LastRunTime)  (result $($i.LastTaskResult); 0 = ok, 267011 = never run)"
        Write-Host "Next run   : $($i.NextRunTime)"
        Write-Host "Command    : $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
    }
    'uninstall' {
        if (Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false
            Write-Host "Removed $TaskPath$TaskName"
        } else { Write-Host 'not installed' }
    }
}
