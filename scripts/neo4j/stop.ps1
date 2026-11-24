param([ValidateRange(1,120)][int]$TimeoutSeconds = 60)
. (Join-Path $PSScriptRoot 'common.ps1')
$snapshot = Get-WtNeo4jSnapshot
if (-not $snapshot.Wrapper) {
    if (@(Get-WtPortOwners).Count) { throw 'No verified wrapper but target ports remain occupied. Refusing to adopt or kill any process.' }
    Write-Output 'No managed new-instance console is running.'
    return
}
$spec = $snapshot.Spec
$token = $snapshot.State.Token
$helper = Join-Path $PSScriptRoot 'send-console-stop.ps1'
Assert-WtNoReparsePath $helper
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')
$stdout = Join-Path $spec.Runtime ("stop-$stamp-$token.out.log")
$stderr = Join-Path $spec.Runtime ("stop-$stamp-$token.err.log")
$arguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
               '-File', ('"' + $helper + '"'), '-ExpectedToken', $token)
$helperProcess = Start-Process -FilePath $spec.PowerShell -ArgumentList $arguments `
    -WorkingDirectory $spec.Home -WindowStyle Hidden -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr -PassThru
# Retain the native handle before the short-lived helper exits. Windows
# PowerShell 5 can otherwise lose its exit code after Start-Process -PassThru.
$helperHandle = $helperProcess.Handle
if ($helperHandle -eq [IntPtr]::Zero) {
    throw "Unable to retain the stop helper process handle; shutdown is not confirmed. Inspect $stderr"
}
if (-not $helperProcess.WaitForExit(30000)) {
    throw "Stop helper did not finish within 30 seconds; no force-kill fallback. Inspect $stderr"
}
$helperProcess.Refresh()
$helperExitCode = $helperProcess.ExitCode
if ($null -eq $helperExitCode) {
    throw "Stop helper exit code is unavailable; shutdown is not confirmed. Inspect $stderr"
}
if ($helperExitCode -ne 0) { throw "CTRL-C helper refused or failed (exit $helperExitCode). Inspect $stderr" }
$report = Get-Content -LiteralPath $stdout -Raw -Encoding UTF8 | ConvertFrom-Json
if ($report.Token -ne $token -or $report.Signal -ne 'CTRL_C_EVENT' -or @($report.Members).Count -eq 0) {
    throw 'Stop helper report is invalid; shutdown is not confirmed.'
}
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    $remaining = @()
    foreach ($member in @($report.Members)) {
        $process = Get-WtProcess ([int]$member.ProcessId)
        if ($process -and (Get-WtCreationStamp $process) -eq $member.CreatedUtc) { $remaining += [int]$member.ProcessId }
    }
    if ($remaining.Count -eq 0) { break }
    Start-Sleep -Milliseconds 500
} while ([DateTime]::UtcNow -lt $deadline)
if ($remaining.Count) {
    throw "Graceful shutdown still pending for verified PIDs $($remaining -join ','); no force-kill fallback."
}
if (@(Get-WtPortOwners).Count) { throw 'Console members exited, but a target port is occupied. No process was killed.' }
Write-Output 'Verified new-instance console exited after CTRL-C. Original instance was not managed.'
