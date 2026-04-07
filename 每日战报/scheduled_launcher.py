import os
import sys
import subprocess
import logging
from datetime import date

# Import the trading calendar checker
from trading_calendar import is_trading_day

# 我们先获取一下 OUTPUT_DIR 以便保存日志
# scheduled_launcher.py 处于 /每日战报 目录下，所以我们需要动态挂载或 import
try:
    from settings import OUTPUT_DIR
except ImportError:
    # 兼容退阶模式
    script_dir = os.path.dirname(os.path.abspath(__file__))
    OUTPUT_DIR = os.path.join(os.path.dirname(script_dir), "每日热点输出")

pipeline_log_path = os.path.join(OUTPUT_DIR, "pipeline.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
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
    
    # 带上 --force-run 参数，因为我们在这里已经做过交易日判断了，
    # 强制让 orchestrator 跑起来，无视它的内置检查（如果有）。
    cmd = [sys.executable, orchestrator_path, '--force-run']
    
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
