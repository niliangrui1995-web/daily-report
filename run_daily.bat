@echo off
setlocal EnableExtensions

set "ROOT_DIR=%~dp0"
set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
set "APP_DIR="
if not defined RUN_DAILY_HOLD_SECONDS set "RUN_DAILY_HOLD_SECONDS=15"

for /d %%D in ("%ROOT_DIR%*") do (
    if exist "%%~fD\scheduled_launcher.py" (
        set "APP_DIR=%%~fD"
    )
)

set NO_PROXY=127.0.0.1,localhost,::1

echo [%date% %time%] VCP Hunter Pipeline Starting...
echo.

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python venv not found: "%PYTHON_EXE%"
    exit /b 3
)

if not defined APP_DIR (
    echo [ERROR] scheduled_launcher.py not found under "%ROOT_DIR%"
    exit /b 2
)

pushd "%APP_DIR%"
if errorlevel 1 (
    echo [ERROR] Failed to enter app dir: "%APP_DIR%"
    exit /b 2
)

"%PYTHON_EXE%" -u "%APP_DIR%\scheduled_launcher.py"
set "EXIT_CODE=%errorlevel%"

popd
echo.
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] Pipeline exited with code: %EXIT_CODE%
) else (
    echo [OK] Done.
)
echo.

if not "%RUN_DAILY_HOLD_SECONDS%"=="0" (
    echo Window will close in %RUN_DAILY_HOLD_SECONDS% seconds...
    powershell -NoProfile -Command "Start-Sleep -Seconds %RUN_DAILY_HOLD_SECONDS%" >nul 2>&1
)

exit /b %EXIT_CODE%
