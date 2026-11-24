Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-WtNeo4jSpec {
    [pscustomobject]@{
        Home = 'D:\neo4j.5\neo4j-water-treatment-v2'
        Conf = 'D:\neo4j.5\neo4j-water-treatment-v2\conf'
        Data = 'D:\new_WaterTreatment-data\neo4j\data'
        Runtime = 'D:\new_WaterTreatment-data\neo4j\run'
        State = 'D:\new_WaterTreatment-data\neo4j\run\console-state.json'
        Wrapper = (Join-Path $PSScriptRoot 'run-console.ps1')
        PowerShell = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
        Ports = @(7475, 7688)
    }
}

function ConvertTo-WtCanonicalPath([string]$Value) {
    [IO.Path]::GetFullPath($Value.Replace('/', '\')).TrimEnd('\').ToLowerInvariant()
}

function Assert-WtNoReparsePath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing reparse point in fixed instance path: $current"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current = $parent
    }
}

function Assert-WtNeo4jLayout {
    $spec = Get-WtNeo4jSpec
    foreach ($path in @($spec.Home, $spec.Conf, $spec.Data, $spec.Runtime, $spec.Wrapper)) {
        Assert-WtNoReparsePath $path
    }
    $launcher = Join-Path $spec.Home 'bin\neo4j.ps1'
    $configuration = Join-Path $spec.Conf 'neo4j.conf'
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "New instance launcher is missing: $launcher" }
    if (-not (Test-Path -LiteralPath $configuration -PathType Leaf)) { throw "New instance configuration is missing: $configuration" }
    # Read only ordinary server configuration, never auth/password files.
    $required = @('server.directories.data', 'server.http.listen_address', 'server.bolt.listen_address')
    $values = @{}
    foreach ($line in [IO.File]::ReadLines($configuration)) {
        if ($line -match '^\s*([^#=\s]+)\s*=\s*(.*?)\s*$' -and $required -contains $Matches[1]) {
            if ($values.ContainsKey($Matches[1])) { throw "Duplicate required isolation setting: $($Matches[1])" }
            $values[$Matches[1]] = $Matches[2]
        }
    }
    foreach ($key in $required) {
        if (-not $values.ContainsKey($key)) { throw "Explicit isolation setting is required: $key" }
    }
    if ((ConvertTo-WtCanonicalPath $values['server.directories.data']) -ne (ConvertTo-WtCanonicalPath $spec.Data)) {
        throw 'Data directory does not match the fixed new instance.'
    }
    if ($values['server.http.listen_address'] -notmatch '^(?:127\.0\.0\.1|localhost|\[::1\])?:7475$' -or
        $values['server.bolt.listen_address'] -notmatch '^(?:127\.0\.0\.1|localhost|\[::1\])?:7688$') {
        throw 'Expected loopback/default HTTP 7475 and Bolt 7688 listen addresses.'
    }
    return $spec
}

function Get-WtProcess([int]$ProcessId) {
    Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
}

function Get-WtCreationStamp($Process) {
    $Process.CreationDate.ToUniversalTime().ToString('o')
}

function Test-WtCommandPath([string]$CommandLine, [string]$Path) {
    if (-not $CommandLine) { return $false }
    $normalized = $CommandLine.Replace('/', '\')
    $expected = [regex]::Escape([IO.Path]::GetFullPath($Path).TrimEnd('\'))
    return [regex]::IsMatch($normalized, $expected + '(?=[\\\s";]|$)', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
}

function Get-WtDescendants([int]$ProcessId) {
    $queue = New-Object 'Collections.Generic.Queue[int]'
    $queue.Enqueue($ProcessId)
    $seen = New-Object 'Collections.Generic.HashSet[int]'
    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$parentId")) {
            if ($seen.Add([int]$child.ProcessId)) {
                $queue.Enqueue([int]$child.ProcessId)
                Write-Output $child
            }
        }
        if ($seen.Count -gt 64) { throw 'Unexpectedly large process tree; refusing to manage it.' }
    }
}

function Get-WtNeo4jSnapshot {
    $spec = Get-WtNeo4jSpec
    Assert-WtNoReparsePath $spec.State
    if (-not (Test-Path -LiteralPath $spec.State -PathType Leaf)) {
        return [pscustomobject]@{ Status = 'not_managed'; Spec = $spec; State = $null; Wrapper = $null; Java = @(); Descendants = @() }
    }
    $state = Get-Content -LiteralPath $spec.State -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.Instance -ne 'neo4j-water-treatment-v2' -or
        (ConvertTo-WtCanonicalPath $state.Home) -ne (ConvertTo-WtCanonicalPath $spec.Home) -or
        (ConvertTo-WtCanonicalPath $state.Conf) -ne (ConvertTo-WtCanonicalPath $spec.Conf) -or
        (ConvertTo-WtCanonicalPath $state.WrapperPath) -ne (ConvertTo-WtCanonicalPath $spec.Wrapper) -or
        $state.Token -notmatch '^[a-f0-9]{32}$') {
        throw 'Unrecognized state record; refusing to manage any process.'
    }
    $wrapper = Get-WtProcess ([int]$state.WrapperPid)
    if (-not $wrapper) {
        return [pscustomobject]@{ Status = 'wrapper_exited'; Spec = $spec; State = $state; Wrapper = $null; Java = @(); Descendants = @() }
    }
    if ((Get-WtCreationStamp $wrapper) -ne $state.WrapperCreatedUtc -or
        (ConvertTo-WtCanonicalPath $wrapper.ExecutablePath) -ne (ConvertTo-WtCanonicalPath $spec.PowerShell) -or
        -not (Test-WtCommandPath $wrapper.CommandLine $spec.Wrapper) -or
        $wrapper.CommandLine -notmatch [regex]::Escape($state.Token)) {
        throw 'Wrapper PID identity mismatch; refusing to manage a reused or unrelated PID.'
    }
    $descendants = @(Get-WtDescendants ([int]$wrapper.ProcessId))
    $java = @($descendants | Where-Object { $_.Name -match '^javaw?\.exe$' })
    foreach ($process in $java) {
        if (-not (Test-WtCommandPath $process.CommandLine $spec.Home) -or
            $process.CommandLine -match 'neo4j-biomass-mof-water' -or
            $process.CommandLine -notmatch 'org\.neo4j\.(?:server|dbms)\.') {
            throw 'Java descendant is not proven to belong to the new Neo4j home.'
        }
    }
    $status = if ($java.Count) { 'running_or_starting' } else { 'wrapper_only' }
    return [pscustomobject]@{ Status = $status; Spec = $spec; State = $state; Wrapper = $wrapper; Java = $java; Descendants = $descendants }
}

function Get-WtPortOwners {
    $spec = Get-WtNeo4jSpec
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $spec.Ports -contains $_.LocalPort } |
      Select-Object LocalAddress, LocalPort, OwningProcess)
}
