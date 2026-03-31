# -*- coding: utf-8 -*-
"""简单测试脚本，单独运行 L1a 情报采集"""

import sys
import os

# 确保能找到同级目录的模块
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from l1a_x_collector import collect_from_x
import logging

def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.info("🚀 开始单独运行 L1a X平台情报采集测试...")
    
    # 强制调用采集，使用较短的回溯窗口测试
    content, stats = collect_from_x(backtrack="24h", max_retries=2)
    
    if stats["status"] == "success":
        print("\n" + "=" * 50)
        print("✅ 采集成功！返回的数据概览：")
        print("=" * 50)
        print(content[:1500] + "\n... (为防刷屏，只显示前1500字符)")
        print("\n" + "=" * 50)
        print(f"📊 耗时: {stats['duration_seconds']}秒, 长度: {stats['content_length']}字")
    else:
        print("\n❌ 采集失败，状态信息：", stats)

if __name__ == "__main__":
    main()
