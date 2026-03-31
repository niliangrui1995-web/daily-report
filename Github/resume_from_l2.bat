@echo off
title 战报接力调度 [跳过L1/L1.5 直达L2]
cd /d "d:\vcp_hunter\每日热点汇总\Github"
python daily_report.py --skip-to-l2
pause
