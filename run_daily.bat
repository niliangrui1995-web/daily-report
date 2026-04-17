@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT_DIR=%~dp0"
for %%I in ("%ROOT_DIR%.") do set "ROOT_DIR=%%~fI\"

set "APP_DIR="
set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
set "SCHEDULED_LAUNCHER="
set "PIPELINE_CLI="
if not defined RUN_DAILY_HOLD_SECONDS set "RUN_DAILY_HOLD_SECONDS=15"
if not defined RUN_DAILY_PREFLIGHT set "RUN_DAILY_PREFLIGHT=off"
if not defined RUN_DAILY_AUTO_RESUME set "RUN_DAILY_AUTO_RESUME=on"
if not defined RUN_DAILY_RESUME_DELAY_SECONDS set "RUN_DAILY_RESUME_DELAY_SECONDS=10"

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

set "PIPELINE_CLI=%APP_DIR%\pipeline_cli.py"
set "SCHEDULED_LAUNCHER=%APP_DIR%\scheduled_launcher.py"

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

set "PRIMARY_COMMAND=run"
if "%~1"=="" (
    call :run_default_entry
) else (
    set "PRIMARY_COMMAND=%~1"
    if /I "%PRIMARY_COMMAND%"=="run" (
        call :run_default_entry
    ) else (
        "%PYTHON_EXE%" -u "%PIPELINE_CLI%" %*
    )
)
set "EXIT_CODE=%errorlevel%"

if not "%EXIT_CODE%"=="0" (
    call :maybe_auto_resume
)

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

:maybe_auto_resume
if /I not "%PRIMARY_COMMAND%"=="run" exit /b 0
if /I "%RUN_DAILY_AUTO_RESUME%"=="0" exit /b 0
if /I "%RUN_DAILY_AUTO_RESUME%"=="off" exit /b 0
if /I "%RUN_DAILY_AUTO_RESUME%"=="false" exit /b 0
if /I "%RUN_DAILY_AUTO_RESUME%"=="skip" exit /b 0
if "%EXIT_CODE%"=="2" exit /b 0

echo.
echo [WARN] Initial run failed with code: %EXIT_CODE%
echo [INFO] Auto-resume will retry once in %RUN_DAILY_RESUME_DELAY_SECONDS% seconds...
if not "%RUN_DAILY_RESUME_DELAY_SECONDS%"=="0" (
    powershell -NoProfile -Command "Start-Sleep -Seconds %RUN_DAILY_RESUME_DELAY_SECONDS%" >nul 2>&1
)

"%PYTHON_EXE%" -u "%PIPELINE_CLI%" resume
set "RESUME_EXIT_CODE=%errorlevel%"
if "%RESUME_EXIT_CODE%"=="0" (
    echo [OK] Auto-resume completed successfully.
    set "EXIT_CODE=0"
) else (
    echo [ERROR] Auto-resume failed with code: %RESUME_EXIT_CODE%
    set "EXIT_CODE=%RESUME_EXIT_CODE%"
)
exit /b 0

:run_default_entry
if exist "%SCHEDULED_LAUNCHER%" (
    "%PYTHON_EXE%" -u "%SCHEDULED_LAUNCHER%"
    exit /b %errorlevel%
)
echo [WARN] scheduled_launcher.py not found, fallback to pipeline_cli.py run
"%PYTHON_EXE%" -u "%PIPELINE_CLI%" run
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
