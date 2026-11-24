param([Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{32}$')][string]$ExpectedToken)
. (Join-Path $PSScriptRoot 'common.ps1')

# This helper owns a separate hidden console. It never detaches the caller's
# console, and it never kills a process or invokes Neo4j's service-based stop.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class WtNeo4jConsoleControl {
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool FreeConsole();
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool AttachConsole(uint processId);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool SetConsoleCtrlHandler(IntPtr handler, bool add);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool GenerateConsoleCtrlEvent(uint ctrlEvent, uint processGroupId);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern uint GetConsoleProcessList([Out] uint[] processList, uint processCount);
}
'@

$snapshot = Get-WtNeo4jSnapshot
if (-not $snapshot.Wrapper -or $snapshot.State.Token -ne $ExpectedToken) {
    throw 'Wrapper identity/token changed; no console signal was sent.'
}
if ($snapshot.Java.Count -eq 0) { throw 'No verified new-home Java descendant yet; retry after startup settles.' }
$attached = $false
try {
    [void][WtNeo4jConsoleControl]::FreeConsole()
    if (-not [WtNeo4jConsoleControl]::AttachConsole([uint32]$snapshot.Wrapper.ProcessId)) {
        throw ('Cannot attach verified wrapper console; Win32 error ' + [Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    $attached = $true
    if (-not [WtNeo4jConsoleControl]::SetConsoleCtrlHandler([IntPtr]::Zero, $true)) {
        throw 'Cannot protect the stop helper from its own CTRL-C event.'
    }
    # Recheck PID/time/path after attaching. Reject any console containing an
    # unrelated process rather than broadcasting into a shared/old console.
    $snapshot = Get-WtNeo4jSnapshot
    if (-not $snapshot.Wrapper -or $snapshot.State.Token -ne $ExpectedToken) {
        throw 'Wrapper changed while attaching; no console signal was sent.'
    }
    $allowed = @([int]$snapshot.Wrapper.ProcessId) + @($snapshot.Descendants | ForEach-Object { [int]$_.ProcessId }) + @([int]$PID)
    $members = New-Object 'UInt32[]' 128
    $count = [WtNeo4jConsoleControl]::GetConsoleProcessList($members, [uint32]$members.Length)
    if ($count -eq 0 -or $count -gt $members.Length) { throw 'Cannot enumerate the complete target console; refusing CTRL-C.' }
    $tracked = @()
    for ($index = 0; $index -lt $count; $index++) {
        $memberId = [int]$members[$index]
        if ($allowed -notcontains $memberId) { throw "Unrelated process $memberId shares the console; refusing CTRL-C." }
        if ($memberId -eq $PID) { continue }
        $member = Get-WtProcess $memberId
        if (-not $member) { throw 'Console membership changed during identity verification; retry.' }
        $tracked += [pscustomobject]@{ ProcessId = $memberId; CreatedUtc = (Get-WtCreationStamp $member) }
    }
    if (@($tracked | Where-Object ProcessId -eq ([int]$snapshot.Wrapper.ProcessId)).Count -ne 1) {
        throw 'Verified wrapper is absent from the attached console; refusing CTRL-C.'
    }
    if (-not [WtNeo4jConsoleControl]::GenerateConsoleCtrlEvent(0, 0)) {
        throw ('CTRL-C delivery failed; Win32 error ' + [Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    Start-Sleep -Milliseconds 400
    $report = [pscustomobject]@{ Token = $ExpectedToken; Signal = 'CTRL_C_EVENT'; Members = $tracked }
} finally {
    if ($attached) { [void][WtNeo4jConsoleControl]::FreeConsole() }
}
$report | ConvertTo-Json -Depth 5
