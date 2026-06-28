"""
公共工具函数 — 跨模块复用的通用逻辑。

当前包含：
- extract_json_from_llm_response(): 从大模型返回中鲁棒提取 JSON

【⚠️核心维护规则 / Agent 指令】
1. 本文件的函数被 p6_mapper / p9_reporter / audit_engine / p3_agent 四方引用，
   修改任何函数签名或返回格式时，必须同时检查所有调用方的兼容性。
"""

import json
import re
from typing import Any

_COMMON_MOJIBAKE_REPLACEMENTS = {
    "\u00e2\u0080\u0098": "\u2018",
    "\u00e2\u0080\u0099": "\u2019",
    "\u00e2\u0080\u009c": "\u201c",
    "\u00e2\u0080\u009d": "\u201d",
    "\u00e2\u0080\u0093": "\u2013",
    "\u00e2\u0080\u0094": "\u2014",
    "\u00e2\u0080\u00a6": "\u2026",
    "\u00e2\u0080\u00a2": "\u2022",
    "\u00e2\u0084\u00a2": "\u2122",
    "\u00e2\u20ac\u02dc": "\u2018",
    "\u00e2\u20ac\u2122": "\u2019",
    "\u00e2\u20ac\u0153": "\u201c",
    "\u00e2\u20ac\u009d": "\u201d",
    "\u00e2\u20ac\ufffd": "\u201d",
    "\u00e2\u20ac\u201c": "\u2013",
    "\u00e2\u20ac\u201d": "\u2014",
    "\u00e2\u20ac\u00a6": "\u2026",
    "\u00e2\u20ac\u00a2": "\u2022",
    "\u00e2\u201e\u00a2": "\u2122",
}


def normalize_common_text_mojibake(text: str) -> str:
    """Repair narrow UTF-8 punctuation mojibake commonly seen in scraped text."""
    if not isinstance(text, str) or "\u00e2" not in text:
        return text
    normalized = text
    for dirty, clean in _COMMON_MOJIBAKE_REPLACEMENTS.items():
        normalized = normalized.replace(dirty, clean)
    return normalized


def normalize_common_mojibake_artifacts(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_common_text_mojibake(value)
    if isinstance(value, dict):
        return {
            normalize_common_text_mojibake(key) if isinstance(key, str) else key: normalize_common_mojibake_artifacts(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalize_common_mojibake_artifacts(item) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_common_mojibake_artifacts(item) for item in value)
    return value


def parse_backtrack_hours(backtrack: str, default: int = 24) -> int:
    """统一解析回溯窗口字符串为小时数。

    Why: p1_rss/p2_x_intel/p3_agent 三处各自用 int(backtrack.replace('h','')) 做简易解析，
         一旦传入非预期格式（如 '3d' 或空串）就会 ValueError 崩溃。
         统一到此函数做鲁棒解析 + 兜底默认值。

    支持格式: '24h', '72h', '240h', '24', 纯数字字符串
    """
    if not backtrack or not backtrack.strip():
        return default
    match = re.match(r'(\d+)\s*h?', backtrack.strip(), re.IGNORECASE)
    return int(match.group(1)) if match else default

def _type_matches(obj: Any, expect_type: str) -> bool:
    if expect_type == 'dict':
        return isinstance(obj, dict)
    if expect_type == 'list':
        return isinstance(obj, list)
    return True

def _strip_opening_code_fence(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r'^```(?:json)?\s*', '', stripped, count=1, flags=re.IGNORECASE)
    if stripped.endswith('```'):
        stripped = stripped[:-3].strip()
    return stripped

def _append_missing_json_closers(text: str) -> str | None:
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == '{':
            stack.append('}')
        elif ch == '[':
            stack.append(']')
        elif ch in '}]':
            if not stack or stack[-1] != ch:
                return None
            stack.pop()
    if in_string:
        return None
    if not stack:
        return text
    return text.rstrip() + '\n' + ''.join(reversed(stack))


def _looks_like_json_string_close(text: str, index: int) -> bool:
    next_index = index + 1
    while next_index < len(text) and text[next_index].isspace():
        next_index += 1
    if next_index >= len(text):
        return True
    return text[next_index] in ',:]}'


def _escape_unescaped_inner_quotes(text: str) -> str:
    chars: list[str] = []
    in_string = False
    escaped = False

    for index, ch in enumerate(text):
        if not in_string:
            chars.append(ch)
            if ch == '"':
                in_string = True
            continue

        if escaped:
            chars.append(ch)
            escaped = False
            continue

        if ch == '\\':
            chars.append(ch)
            escaped = True
            continue

        if ch == '"':
            if _looks_like_json_string_close(text, index):
                chars.append(ch)
                in_string = False
            else:
                chars.append('\\"')
            continue

        chars.append(ch)

    return ''.join(chars)


def extract_json_from_llm_response(raw_text: str, expect_type: str = 'dict', allow_partial: bool = False) -> Any:
    if not raw_text or not raw_text.strip():
        raise json.JSONDecodeError('空文本', raw_text if raw_text else '', 0)
        
    candidates = []
    code_block_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw_text, re.DOTALL)
    if code_block_match:
        candidates.append(code_block_match.group(1).strip())
    candidates.append(_strip_opening_code_fence(raw_text))
    candidates.append(raw_text.strip())
        
    for json_str in candidates:
        try:
            result = json.loads(json_str)
            if _type_matches(result, expect_type):
                return result
        except json.JSONDecodeError:
            pass
        
    open_bracket = '{' if expect_type == 'dict' else '['
    close_bracket = '}' if expect_type == 'dict' else ']'
    
    for json_str in candidates:
        start_idx = json_str.find(open_bracket)
        end_idx = json_str.rfind(close_bracket)
        
        if start_idx >= 0 and end_idx > start_idx:
            try:
                result = json.loads(json_str[start_idx:end_idx + 1])
                if _type_matches(result, expect_type):
                    return result
            except json.JSONDecodeError:
                pass

    if allow_partial:
        for json_str in candidates:
            start_idx = json_str.find(open_bracket)
            if start_idx < 0:
                continue
            body = json_str[start_idx:]
            for candidate in (body, _escape_unescaped_inner_quotes(body)):
                repaired = _append_missing_json_closers(candidate)
                if not repaired:
                    continue
                try:
                    result = json.loads(repaired)
                    if _type_matches(result, expect_type):
                        return result
                except json.JSONDecodeError:
                    pass
                
    raise json.JSONDecodeError(f'无法从大模型返回中提取 {expect_type} 类型 JSON', raw_text[:200], 0)
