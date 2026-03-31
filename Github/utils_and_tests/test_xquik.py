import os
import sys
import logging

# 配置日志输出到控制台，以便看到 fetch_x_influencers 的执行情况
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 引入被测函数
sys.path.append(r"d:\vcp_hunter\每日热点汇总\Github")
try:
    from daily_report import fetch_x_influencers, XQUIK_API_KEY
    print(f"XQUIK_API_KEY loaded: {XQUIK_API_KEY[:10]}...")
except Exception as e:
    print(f"Import Error: {e}")
    sys.exit(1)

if __name__ == "__main__":
    print("-" * 50)
    print("Testing Xquik API direct grab...")
    result = fetch_x_influencers()
    print("-" * 50)
    print("Test finished.")
    if result:
        print("Scrape Success! Extracted Snippets:")
        print(result["text"])
    else:
        print("Scrape Failed or returned Nothing.")
