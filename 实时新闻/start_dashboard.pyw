"""
start_dashboard.pyw — 一键启动脚本
双击后自动启动后台采集器 + Streamlit 前台大屏（浏览器自动弹出）。
"""
import subprocess
import os
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# .pyw 用的是 pythonw.exe，需要换成 python.exe 才有控制台输出
PYTHON = sys.executable.replace("pythonw.exe", "python.exe")

# 启动后台采集器（最小化窗口）
spider_cmd = f'start "Spider Worker" /min "{PYTHON}" "{os.path.join(PROJECT_DIR, "spider_worker.py")}"'
subprocess.Popen(spider_cmd, shell=True, cwd=PROJECT_DIR)

time.sleep(3)

# 启动 Streamlit 前台大屏（不加 headless，让浏览器自动弹出）
streamlit_cmd = f'start "Dashboard" "{PYTHON}" -m streamlit run "{os.path.join(PROJECT_DIR, "dashboard_ui.py")}"'
subprocess.Popen(streamlit_cmd, shell=True, cwd=PROJECT_DIR)
