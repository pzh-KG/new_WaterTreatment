. (Join-Path $PSScriptRoot 'common.ps1')
$snapshot = Get-WtNeo4jSnapshot
$owners = @(Get-WtPortOwners)
$javaIds = @($snapshot.Java | ForEach-Object { [int]$_.ProcessId })
$portsOwned = @($owners | Where-Object { $javaIds -contains [int]$_.OwningProcess })
[pscustomobject]@{
    Instance = 'neo4j-water-treatment-v2'
    Home = $snapshot.Spec.Home
    Conf = $snapshot.Spec.Conf
    State = $snapshot.Status
    WrapperPid = if ($snapshot.Wrapper) { [int]$snapshot.Wrapper.ProcessId } else { $null }
    JavaPids = $javaIds
    HttpReady = [bool](@($portsOwned | Where-Object LocalPort -eq 7475).Count)
    BoltReady = [bool](@($portsOwned | Where-Object LocalPort -eq 7688).Count)
    PortOwners = $owners
    Note = 'Port readiness is not database authentication or query validation.'
} | ConvertTo-Json -Depth 5
