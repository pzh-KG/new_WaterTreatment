param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonExecutable = Join-Path $projectDirectory '.venv\Scripts\python.exe'
$configurationFile = Join-Path $projectDirectory 'config\project.local.json'

if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw 'Project Python environment is missing. Follow the environment setup in README.md.'
}
if (-not (Test-Path -LiteralPath $configurationFile -PathType Leaf)) {
    throw 'Missing config\project.local.json. Configure the isolated data and source folders first.'
}

Write-Host "WaterTreatment local workbench: http://127.0.0.1:$Port"
Write-Host 'Keep this terminal open. Press Ctrl+C here to stop the service.'
$serviceExitCode = 1
Push-Location -LiteralPath $projectDirectory
try {
    & $pythonExecutable -m watertreatment --config $configurationFile web --host 127.0.0.1 --port $Port
    $serviceExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $serviceExitCode
