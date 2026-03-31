@echo off
chcp 65001 >nul
title VCP Hunter 实时情报雷达

echo ============================================================
echo   🚨 VCP Hunter 实时情报雷达 — 一键启动
echo ============================================================
echo.

cd /d "d:\vcp_hunter\每日热点汇总\实时新闻"

echo [1/2] 启动后台采集器 (spider_worker.py)...
start "Spider Worker" /min cmd /k "cd /d d:\vcp_hunter\每日热点汇总\实时新闻 && python spider_worker.py"

echo [2/2] 启动 Streamlit 前台大屏 (dashboard_ui.py)...
timeout /t 3 /nobreak >nul
start "Dashboard UI" cmd /k "cd /d d:\vcp_hunter\每日热点汇总\实时新闻 && streamlit run dashboard_ui.py --server.headless true"

echo.
echo ✅ 全部启动完毕！
echo    后台采集器已最小化运行
echo    Streamlit 大屏将在浏览器中自动打开
echo.
echo 按任意键关闭此窗口...
pause >nul
