0x5465c0# 文件：checkpoint.cpython-310.pyc (Python 3.10)

'''断点检测 — 自适应扫描已完成的层级。

从 daily_report.py 拆分。

注意：检测到的文件可能在读取前被删除（竞态条件），
orchestrator 调用方应在 open() 时捕获 FileNotFoundError。

【⚠️核心维护规则 / Agent 指令】
1. 这是一个极其敏感的状态判定机，纯靠 glob 扫描本地文件系统来标定当前流水线所处的阶段。修改时严禁引入会引发阻塞或耗时过长的网络 IO 操作。
2. 任何新增的断点状态定义（如 p8_resume），必须向后兼容且与 `orchestrator.py` 中的 `_resume_dispatch` 路由表严格对齐配套。
3. 注意防范磁盘断点文件的竞态条件，检测到的文件可能在读取瞬间被用户手贱删掉，一定要防御性地包容 FileNotFoundError。
'''
import os
from glob import glob
from settings import get_today_output_dir

def detect_checkpoint():
    '''扫描当天输出目录，检测已完成的最远层级。

    Returns:
        dict: stage ("none"/"p1"/"p5"/"p6"/"p8_resume"/"p9_done"), files (各阶段最新文件)
    '''
    today_dir = get_today_output_dir()
    ckpt = {
        'stage': 'none',
        'files': { } }
    war_files = sorted(glob(os.path.join(today_dir, '战报_*.md')), reverse=True)
    if war_files:
        ckpt['stage'] = 'p9_done'
        ckpt['files']['war_report'] = war_files[0]
        return ckpt
    p6_files = sorted(glob(os.path.join(today_dir, 'P6_映射_*.md')), reverse=True)
    if p6_files:
        ckpt['stage'] = 'p6'
        ckpt['files']['p6'] = p6_files[0]
    p5_files = sorted(glob(os.path.join(today_dir, 'P5_审计_*.md')), reverse=True)
    if p5_files:
        ckpt['files']['p5'] = p5_files[0]
        if ckpt['stage'] == 'none':
            ckpt['stage'] = 'p5'
    p1_files = sorted(glob(os.path.join(today_dir, 'P1_*原始数据_*.json')), reverse=True)
    if p1_files:
        ckpt['files']['p1'] = p1_files[0]
        if ckpt['stage'] == 'none':
            ckpt['stage'] = 'p1'
    p2_files = sorted(glob(os.path.join(today_dir, 'P2_X平台情报_*.md')), reverse=True)
    if p2_files:
        ckpt['files']['p2'] = p2_files[0]
    p3_files = sorted(glob(os.path.join(today_dir, 'P3_机动情报_*.md')), reverse=True)
    if p3_files:
        ckpt['files']['p3'] = p3_files[0]
    p7_files = sorted(glob(os.path.join(today_dir, 'P7_深度研究_*.md')), reverse=True)
    if p7_files:
        ckpt['files']['p7'] = p7_files[0]
    p75_files = sorted(glob(os.path.join(today_dir, 'P7.5_补充审计_*.md')), reverse=True)
    if p75_files:
        ckpt['files']['p75'] = p75_files[0]
    if ckpt['stage'] == 'p6' and 'p7' in ckpt['files']:
        ckpt['stage'] = 'p8_resume'
    return ckpt

