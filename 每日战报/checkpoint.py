"""断点检测 — 自适应扫描已完成的层级。

从 daily_report.py 拆分。

注意：检测到的文件可能在读取前被删除（竞态条件），
orchestrator 调用方应在 open() 时捕获 FileNotFoundError。

【⚠️核心维护规则 / Agent 指令】
1. 这是一个极其敏感的状态判定机，纯靠 glob 扫描本地文件系统来标定当前流水线所处的阶段。修改时严禁引入会引发阻塞或耗时过长的网络 IO 操作。
2. 任何新增的断点状态定义（如 p8_resume），必须向后兼容且与 `orchestrator.py` 中的 `_resume_dispatch` 路由表严格对齐配套。
3. 注意防范磁盘断点文件的竞态条件，检测到的文件可能在读取瞬间被用户手贱删掉，一定要防御性地包容 FileNotFoundError。
"""
import os
from glob import glob

from artifact_store import extract_timestamp_from_name, load_run_manifest
from settings import get_today_output_dir


def _pick_existing_file(path: str) -> str:
    return path if path and os.path.exists(path) else ''


def _load_files_from_manifest(ckpt: dict, manifest: dict) -> None:
    stages = manifest.get('stages', {})
    alias_map = {
        'p1': 'p1',
        'p2': 'p2',
        'p3': 'p3',
        'p4': 'p4',
        'p5': 'p5',
        'p6': 'p6',
        'p7': 'p7',
        'p7a': 'p7a',
        'p75': 'p75',
        'p8': 'p8',
        'p9': 'war_report',
    }
    for stage_name, file_key in alias_map.items():
        payload = stages.get(stage_name, {})
        files = payload.get('files', {})
        preferred = files.get('md') or files.get('json') or ''
        existing = _pick_existing_file(preferred)
        if existing:
            ckpt['files'][file_key] = existing


def detect_checkpoint():
    """扫描当天输出目录，检测已完成的最远层级。

    Returns:
        dict: stage ("none"/"p1"/"p5"/"p6"/"p8_resume"/"p9_resume"/"p9_done"), files (各阶段最新文件)
    """
    today_dir = get_today_output_dir()
    ckpt = {
        'stage': 'none',
        'files': {},
        'run_timestamp': '',
    }

    manifest = load_run_manifest()
    if manifest:
        ckpt['run_timestamp'] = manifest.get('run_timestamp', '')
        _load_files_from_manifest(ckpt, manifest)

    war_files = sorted(glob(os.path.join(today_dir, '战报_*.md')), reverse=True)
    if war_files:
        ckpt['stage'] = 'p9_done'
        ckpt['files']['war_report'] = war_files[0]
        ckpt['run_timestamp'] = ckpt['run_timestamp'] or extract_timestamp_from_name(war_files[0])
        return ckpt

    p6_files = sorted(glob(os.path.join(today_dir, 'P6_映射_*.md')), reverse=True)
    if p6_files and 'p6' not in ckpt['files']:
        ckpt['stage'] = 'p6'
        ckpt['files']['p6'] = p6_files[0]

    p5_files = sorted(glob(os.path.join(today_dir, 'P5_审计_*.md')), reverse=True)
    if p5_files and 'p5' not in ckpt['files']:
        ckpt['files']['p5'] = p5_files[0]
        if ckpt['stage'] == 'none':
            ckpt['stage'] = 'p5'

    p1_files = sorted(glob(os.path.join(today_dir, 'P1_*原始数据_*.json')), reverse=True)
    if p1_files and 'p1' not in ckpt['files']:
        ckpt['files']['p1'] = p1_files[0]
        if ckpt['stage'] == 'none':
            ckpt['stage'] = 'p1'

    p1_md_files = sorted(glob(os.path.join(today_dir, 'P1_*原始数据_*.md')), reverse=True)
    if p1_md_files and 'p1_md' not in ckpt['files']:
        ckpt['files']['p1_md'] = p1_md_files[0]

    p2_files = sorted(glob(os.path.join(today_dir, 'P2_X平台情报_*.md')), reverse=True)
    if p2_files and 'p2' not in ckpt['files']:
        ckpt['files']['p2'] = p2_files[0]
    p3_files = sorted(glob(os.path.join(today_dir, 'P3_机动情报_*.md')), reverse=True)
    if p3_files and 'p3' not in ckpt['files']:
        ckpt['files']['p3'] = p3_files[0]
    p4_files = sorted(glob(os.path.join(today_dir, 'P4_海外行情_*.md')), reverse=True)
    if p4_files and 'p4' not in ckpt['files']:
        ckpt['files']['p4'] = p4_files[0]

    p7_files = sorted(glob(os.path.join(today_dir, 'P7_深度研究_*.md')), reverse=True)
    if p7_files and 'p7' not in ckpt['files']:
        ckpt['files']['p7'] = p7_files[0]
    p7a_files = sorted(glob(os.path.join(today_dir, 'P7a_深研审计_*.md')), reverse=True)
    if p7a_files and 'p7a' not in ckpt['files']:
        ckpt['files']['p7a'] = p7a_files[0]
    p75_files = sorted(glob(os.path.join(today_dir, 'P7.5_补充审计_*.md')), reverse=True)
    if p75_files and 'p75' not in ckpt['files']:
        ckpt['files']['p75'] = p75_files[0]

    p8_files = sorted(glob(os.path.join(today_dir, 'P8_技术面_*.md')), reverse=True)
    if p8_files and 'p8' not in ckpt['files']:
        ckpt['files']['p8'] = p8_files[0]

    if not ckpt['run_timestamp']:
        for candidate in ckpt['files'].values():
            ckpt['run_timestamp'] = extract_timestamp_from_name(candidate)
            if ckpt['run_timestamp']:
                break

    if 'p8' in ckpt['files']:
        ckpt['stage'] = 'p9_resume'
    elif ckpt['stage'] == 'p6' and ('p7' in ckpt['files'] or 'p7a' in ckpt['files']):
        ckpt['stage'] = 'p8_resume'
    return ckpt
