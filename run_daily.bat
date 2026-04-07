@echo off
cd /d "%~dp0每日战报"
set NO_PROXY=127.0.0.1,localhost,::1

echo [%date% %time%] VCP Hunter Pipeline Starting...
echo.

"%~dp0.venv\Scripts\python.exe" -u scheduled_launcher.py

echo.
if %errorlevel% neq 0 (
    echo [ERROR] Pipeline exited with code: %errorlevel%
) else (
    echo [OK] Done.
)
echo.

pause
