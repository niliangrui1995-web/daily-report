@echo off
setlocal EnableExtensions

set "ROOT_DIR=%~dp0"
for %%I in ("%ROOT_DIR%.") do set "ROOT_DIR=%%~fI\"

set "APP_DIR="
set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
if not defined RUN_DAILY_HOLD_SECONDS set "RUN_DAILY_HOLD_SECONDS=15"

for /d %%D in ("%ROOT_DIR%*") do (
    if exist "%%~fD\pipeline_cli.py" (
        set "APP_DIR=%%~fD"
    )
)

set "NO_PROXY=127.0.0.1,localhost,::1"

echo [%date% %time%] VCP Hunter Pipeline Starting...
echo.

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python venv not found: "%PYTHON_EXE%"
    goto :fail
)

if not defined APP_DIR (
    echo [ERROR] pipeline_cli.py not found under "%ROOT_DIR%"
    goto :fail
)

pushd "%APP_DIR%"
if errorlevel 1 (
    echo [ERROR] Failed to enter app dir: "%APP_DIR%"
    goto :fail
)

if "%~1"=="" (
    "%PYTHON_EXE%" -u "%APP_DIR%\pipeline_cli.py" run
) else (
    "%PYTHON_EXE%" -u "%APP_DIR%\pipeline_cli.py" %*
)
set "EXIT_CODE=%errorlevel%"

popd
echo.
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] Pipeline exited with code: %EXIT_CODE%
) else (
    echo [OK] Done.
)
echo.
goto :hold

:fail
set "EXIT_CODE=%errorlevel%"
if "%EXIT_CODE%"=="0" set "EXIT_CODE=1"
echo.

:hold
if not "%RUN_DAILY_HOLD_SECONDS%"=="0" (
    echo Window will close in %RUN_DAILY_HOLD_SECONDS% seconds...
    powershell -NoProfile -Command "Start-Sleep -Seconds %RUN_DAILY_HOLD_SECONDS%" >nul 2>&1
)

exit /b %EXIT_CODE%
