param([ValidateRange(1,60)][int]$StartupWaitSeconds = 20)
. (Join-Path $PSScriptRoot 'common.ps1')
$spec = Assert-WtNeo4jLayout
$snapshot = Get-WtNeo4jSnapshot
if ($snapshot.Wrapper) { throw 'A verified wrapper already exists. Use status.ps1; do not start another instance.' }
if (@(Get-WtPortOwners).Count) { throw 'Port 7475 or 7688 is occupied; no process will be stopped automatically.' }
[IO.Directory]::CreateDirectory($spec.Runtime) | Out-Null
$token = [guid]::NewGuid().ToString('N')
$arguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
               '-File', ('"' + $spec.Wrapper + '"'), '-LaunchToken', $token)
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')
$stdout = Join-Path $spec.Runtime ("console-$stamp-$token.out.log")
$stderr = Join-Path $spec.Runtime ("console-$stamp-$token.err.log")
$process = Start-Process -FilePath $spec.PowerShell -ArgumentList $arguments -WorkingDirectory $spec.Home `
    -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
$wrapper = Get-WtProcess $process.Id
if (-not $wrapper) { throw "Wrapper exited immediately. Inspect $stderr" }
$state = [ordered]@{ Instance = 'neo4j-water-treatment-v2'; Home = $spec.Home; Conf = $spec.Conf;
    WrapperPath = $spec.Wrapper; WrapperPid = [int]$process.Id; WrapperCreatedUtc = (Get-WtCreationStamp $wrapper);
    Token = $token; Stdout = $stdout; Stderr = $stderr; RecordedUtc = [DateTime]::UtcNow.ToString('o') }
$temporary = Join-Path $spec.Runtime ("state-$token.tmp")
[IO.File]::WriteAllText($temporary, ($state | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
Move-Item -LiteralPath $temporary -Destination $spec.State -Force
$deadline = [DateTime]::UtcNow.AddSeconds($StartupWaitSeconds)
do {
    $snapshot = Get-WtNeo4jSnapshot
    if (-not $snapshot.Wrapper) { throw "New wrapper exited. Inspect $stderr" }
    if ($snapshot.Java.Count -gt 0) { break }
    Start-Sleep -Milliseconds 300
} while ([DateTime]::UtcNow -lt $deadline)
& (Join-Path $PSScriptRoot 'status.ps1')
