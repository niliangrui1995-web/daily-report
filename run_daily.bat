@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT_DIR=%~dp0"
for %%I in ("%ROOT_DIR%.") do set "ROOT_DIR=%%~fI\"

set "APP_DIR="
set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
if not defined RUN_DAILY_HOLD_SECONDS set "RUN_DAILY_HOLD_SECONDS=15"
if not defined RUN_DAILY_PREFLIGHT set "RUN_DAILY_PREFLIGHT=off"

for /d %%D in ("%ROOT_DIR%*") do (
    if exist "%%~fD\pipeline_cli.py" (
        set "APP_DIR=%%~fD"
    )
)

call :merge_no_proxy

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

call :maybe_preflight
if errorlevel 1 (
    set "EXIT_CODE=!errorlevel!"
    popd
    echo.
    echo [ERROR] Preflight failed with code: !EXIT_CODE!
    echo.
    goto :hold
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

:maybe_preflight
set "PREFLIGHT_MODE=%RUN_DAILY_PREFLIGHT%"
if /I "%PREFLIGHT_MODE%"=="0" exit /b 0
if /I "%PREFLIGHT_MODE%"=="off" exit /b 0
if /I "%PREFLIGHT_MODE%"=="false" exit /b 0
if /I "%PREFLIGHT_MODE%"=="skip" exit /b 0

set "PREFLIGHT_COMMAND=doctor"
if /I "%PREFLIGHT_MODE%"=="strict" set "PREFLIGHT_COMMAND=preflight"
if /I "%PREFLIGHT_MODE%"=="preflight" set "PREFLIGHT_COMMAND=preflight"

echo [%date% %time%] Preflight: %PREFLIGHT_COMMAND%
"%PYTHON_EXE%" -u "%APP_DIR%\pipeline_cli.py" %PREFLIGHT_COMMAND%
exit /b %errorlevel%

:merge_no_proxy
set "MERGED_NO_PROXY="
set "NO_PROXY_INPUT=%NO_PROXY%"
set "NO_PROXY_INPUT=%NO_PROXY_INPUT:,= %"
for %%H in (%NO_PROXY_INPUT% 127.0.0.1 localhost ::1) do (
    call :append_no_proxy "%%~H"
)
set "NO_PROXY=%MERGED_NO_PROXY%"
set "no_proxy=%MERGED_NO_PROXY%"
exit /b 0

:append_no_proxy
set "HOST=%~1"
if "!HOST!"=="" exit /b 0
if defined MERGED_NO_PROXY (
    echo ,!MERGED_NO_PROXY!, | findstr /I /C:",!HOST!," >nul
    if not errorlevel 1 exit /b 0
    set "MERGED_NO_PROXY=!MERGED_NO_PROXY!,!HOST!"
) else (
    set "MERGED_NO_PROXY=!HOST!"
)
exit /b 0
