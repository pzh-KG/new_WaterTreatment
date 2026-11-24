param([Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{32}$')][string]$LaunchToken)
. (Join-Path $PSScriptRoot 'common.ps1')
$spec = Assert-WtNeo4jLayout
# Child-process environment only. Never change user/machine environment variables.
$env:NEO4J_HOME = $spec.Home
$env:NEO4J_CONF = $spec.Conf
Set-Location -LiteralPath $spec.Home
& (Join-Path $spec.Home 'bin\neo4j.ps1') console
exit $LASTEXITCODE
