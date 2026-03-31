@echo off
chcp 65001 >nul 2>nul

REM === VCP Hunter Real-time Dashboard Launcher ===

set "PROJECT_DIR=d:\vcp_hunter\每日热点汇总\实时新闻"

echo ============================================================
echo   VCP Hunter Dashboard - Starting...
echo ============================================================

cd /d "%PROJECT_DIR%"

echo [1/2] Starting spider_worker...
start "" /min cmd /c "chcp 65001 >nul 2>nul && cd /d "%PROJECT_DIR%" && python spider_worker.py"

echo [2/2] Starting Streamlit dashboard...
timeout /t 3 /nobreak >nul
start "" cmd /c "chcp 65001 >nul 2>nul && cd /d "%PROJECT_DIR%" && streamlit run dashboard_ui.py --server.headless true"

echo.
echo Done! Browser will open shortly.
timeout /t 3 >nul
exit
