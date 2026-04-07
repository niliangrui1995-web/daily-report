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

def _type_matches(obj: Any, expect_type: str) -> bool:
    if expect_type == 'dict':
        return isinstance(obj, dict)
    if expect_type == 'list':
        return isinstance(obj, list)
    return True

def extract_json_from_llm_response(raw_text: str, expect_type: str = 'dict') -> Any:
    if not raw_text or not raw_text.strip():
        raise json.JSONDecodeError('空文本', raw_text if raw_text else '', 0)
        
    code_block_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw_text, re.DOTALL)
    if code_block_match:
        json_str = code_block_match.group(1).strip()
    else:
        json_str = raw_text.strip()
        
    try:
        result = json.loads(json_str)
        if _type_matches(result, expect_type):
            return result
    except json.JSONDecodeError:
        pass
        
    open_bracket = '{' if expect_type == 'dict' else '['
    close_bracket = '}' if expect_type == 'dict' else ']'
    
    start_idx = json_str.find(open_bracket)
    end_idx = json_str.rfind(close_bracket)
    
    if start_idx >= 0 and end_idx > start_idx:
        try:
            result = json.loads(json_str[start_idx:end_idx + 1])
            if _type_matches(result, expect_type):
                return result
        except json.JSONDecodeError:
            pass
            
    if code_block_match:
        start_idx = raw_text.find(open_bracket)
        end_idx = raw_text.rfind(close_bracket)
        
        if start_idx >= 0 and end_idx > start_idx:
            try:
                result = json.loads(raw_text[start_idx:end_idx + 1])
                if _type_matches(result, expect_type):
                    return result
            except json.JSONDecodeError:
                pass
                
    raise json.JSONDecodeError(f'无法从大模型返回中提取 {expect_type} 类型 JSON', raw_text[:200], 0)
