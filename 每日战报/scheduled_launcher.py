import os
import sys
import subprocess
import logging
from datetime import date

# Import the trading calendar checker
from trading_calendar import is_trading_day


def _configure_stdio() -> None:
    """避免 Windows 控制台默认编码导致 emoji/中文日志写出失败。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

# 我们先获取一下 OUTPUT_DIR 以便保存日志
# scheduled_launcher.py 处于 /每日战报 目录下，所以我们需要动态挂载或 import
try:
    from settings import OUTPUT_DIR
except ImportError:
    # 兼容退阶模式
    script_dir = os.path.dirname(os.path.abspath(__file__))
    OUTPUT_DIR = os.path.join(os.path.dirname(script_dir), "每日热点输出")

_configure_stdio()

pipeline_log_path = os.path.join(OUTPUT_DIR, "pipeline.log")

logging.basicConfig(
    level=logging.INFO,
    # Why: 与 orchestrator.py 统一日志格式，避免两个进程写入同一文件时输出不一致
    format='%(asctime)s - %(levelname)s - %(message)s',
    force=True,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(pipeline_log_path, encoding='utf-8', mode='a')
    ]
)

def main():
    today = date.today()
    
    # 1. 拦截层：判断是否为法定节假日或周末
    if not is_trading_day(today):
        logging.info(f"📅 今日 ({today}) 为非交易日，自动化战报流水线触发物理熔断，安全退出。避免了大模型空烧。")
        sys.exit(0)
        
    logging.info(f"✅ 今日 ({today}) 为正常交易日，门神验证放行！开始唤醒主脑...")
    
    # 2. 拉起层：验证通过，准备执行
    script_dir = os.path.dirname(os.path.abspath(__file__))
    orchestrator_path = os.path.join(script_dir, 'orchestrator.py')
    
    # 移除 --force-run 参数，避免破坏 orchestrator 的断点续跑机制
    cmd = [sys.executable, orchestrator_path]
    
    logging.info(f"🚀 正式发射指令: {' '.join(cmd)}\n{'-'*60}\n")
    
    try:
        # 直接透传输出流，不阻塞
        result = subprocess.run(cmd, cwd=script_dir)
        sys.exit(result.returncode)
    except Exception as e:
        logging.error(f"❌ 灾难性错误：无法拉起主系统编排器，失败原因: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
