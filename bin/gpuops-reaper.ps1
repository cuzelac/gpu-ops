<#
.SYNOPSIS
  One reaper pass: delete gpu-ops machines more than 10 minutes past their deadline.
  Run by the "gpu-ops reaper" scheduled task (see gpuops-reaper-task.ps1); safe to run by hand.

.DESCRIPTION
  - Calls `gpu-ops reap --json` inside WSL. The RunPod key comes from the 1Password service account,
    so no prompt is needed (see docs/unattended.md).
  - Logs every pass to %LOCALAPPDATA%\gpu-ops\reaper.log (last 2000 lines kept).
  - Shows a Windows notification only when it deleted something or when it failed.
  Must run under Windows PowerShell 5.1 (powershell.exe): notifications use its WinRT support.

.PARAMETER DryRun      Report what would be deleted; delete nothing.
.PARAMETER TestToast   Only show a test notification, to check notifications reach you.
#>
param([switch]$DryRun, [switch]$TestToast)
$ErrorActionPreference = 'Continue'

$logDir = Join-Path $env:LOCALAPPDATA 'gpu-ops'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'reaper.log'

function Write-Log([string]$msg) {
    "$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') $msg" | Add-Content -Encoding UTF8 -Path $log
}

function Show-Toast([string]$title, [string]$body) {
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
        $t = [System.Security.SecurityElement]::Escape($title)
        $b = [System.Security.SecurityElement]::Escape($body)
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual></toast>")
        $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
            [Windows.UI.Notifications.ToastNotification]::new($xml))
    } catch {
        Write-Log "notification failed: $($_.Exception.Message)"
    }
}

if ($TestToast) {
    Show-Toast 'gpu-ops reaper' 'Test notification: the reaper can reach you.'
    Write-Log 'test notification sent'
    exit 0
}

$reapArgs = if ($DryRun) { 'reap --json --dry-run' } else { 'reap --json' }
# stdout = the JSON result; stderr goes to a WSL-side log so it can't corrupt the JSON
# full path, not bare `gpu-ops`: WSL appends the Windows PATH, which may contain this bin folder.
# GPUOPS_NO_DESKTOP_FALLBACK=1: if the service account fails, fail (and notify) instead of popping a
# 1Password password prompt at whoever is, or isn't, at the screen.
$out = & wsl.exe -e bash -lc "GPUOPS_NO_DESKTOP_FALLBACK=1 ~/.local/bin/gpu-ops $reapArgs 2>>~/.local/state/gpu-ops/reaper-stderr.log"
$code = $LASTEXITCODE
$text = ($out | Out-String).Trim()

try {
    $result = $text | ConvertFrom-Json
} catch {
    $result = $null
}

if ($code -eq 0 -and $null -ne $result) {
    $reaped = @($result.reaped)
    if ($reaped.Count -eq 0) {
        Write-Log "ok: nothing past its deadline$(if ($DryRun) { ' (dry run)' })"
    } else {
        $names = ($reaped | ForEach-Object { "$($_.id) ($($_.reason))" }) -join '; '
        $verb = if ($DryRun) { 'would delete' } else { 'deleted' }
        Write-Log "REAPED: $verb $($reaped.Count): $names"
        # Name the runs: a detached run reaped before `gpu-ops collect` has lost its outputs
        # for good, and that is worth telling the human about. The id is read out of the
        # instance's own name, not the ledger -- the reaper never reads local state (design.md s4).
        $runs = (@($reaped | ForEach-Object { $_.run_id } | Where-Object { $_ }) -join ', ')
        $detail = if ($runs) { " Runs: $runs. If one was detached and not yet collected, its outputs are lost." } else { '' }
        Show-Toast 'gpu-ops reaper' "$($reaped.Count) machine(s) were past their deadline and $verb. The run that started them did not clean up; check its record.$detail"
    }
} else {
    $why = if ($null -ne $result -and $result.error) { $result.error } elseif ($text) { $text.Substring(0, [Math]::Min(300, $text.Length)) } else { 'no output' }
    Write-Log "FAILED (exit $code): $why"
    $hint = switch ($code) {
        3       { 'Could not read the RunPod key via the 1Password service account. Check the system clock first (a wrong clock breaks service-account sign-in); details in reaper.log.' }
        10      { 'A machine could not be deleted and may still be billing.' }
        default { 'A machine may still be billing.' }
    }
    Show-Toast "gpu-ops reaper FAILED (exit $code)" "$hint Run 'gpu-ops ps' or check the RunPod console."
}

# keep the log bounded
$lines = Get-Content -Path $log -Encoding UTF8
if ($lines.Count -gt 2000) { $lines | Select-Object -Last 2000 | Set-Content -Path $log -Encoding UTF8 }
exit $code
