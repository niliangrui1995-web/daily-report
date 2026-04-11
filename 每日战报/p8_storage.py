"""P8 artifact persistence helpers."""

from __future__ import annotations

import logging

from artifact_store import update_run_manifest, write_json_artifact, write_text_artifact


def persist_p8_outputs(markdown_text: str, json_data: dict, *, status: str, metrics: dict) -> str:
    md_path = ''
    json_path = ''

    try:
        md_path = write_text_artifact('P8_技术面', markdown_text)
    except Exception as e:
        logging.error(f'[P8] Markdown 写入失败: {e}')

    try:
        json_path = write_json_artifact('P8_技术面', json_data)
    except Exception as e:
        logging.error(f'[P8] JSON 写入失败: {e}')

    files = {}
    if md_path:
        files['md'] = md_path
    if json_path:
        files['json'] = json_path

    update_run_manifest('p8', status=status, files=files or None, metrics=metrics)
    return markdown_text
