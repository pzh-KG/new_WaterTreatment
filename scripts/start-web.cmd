@echo off
setlocal
set "WT_WEB_PROJECT=%~dp0.."
set "WT_WEB_PYTHON=%~dp0..\.venv\Scripts\python.exe"
set "WT_WEB_CONFIG=%~dp0..\config\project.local.json"

if not exist "%WT_WEB_PYTHON%" (
    echo Project Python environment is missing. See README.md.
    pause
    exit /b 1
)
if not exist "%WT_WEB_CONFIG%" (
    echo Missing config\project.local.json. See README.md.
    pause
    exit /b 1
)

echo WaterTreatment local workbench: http://127.0.0.1:8765
echo Keep this window open. Press Ctrl+C here to stop the service.
pushd "%WT_WEB_PROJECT%"
"%WT_WEB_PYTHON%" -m watertreatment --config "%WT_WEB_CONFIG%" web --host 127.0.0.1 --port 8765
set "WT_WEB_EXIT=%ERRORLEVEL%"
popd
if not "%WT_WEB_EXIT%"=="0" pause
exit /b %WT_WEB_EXIT%
