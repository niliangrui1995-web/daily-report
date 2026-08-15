__doc__ = '\n原生 SDK 适配器 — 伪装成 LangChain ChatOpenAI\n\n核心原理：\n  外部接口完全兼容 LangChain 的 .invoke(messages) 方法\n  内部根据模型名自动选择 Anthropic SDK 或 Google GenAI SDK 直连\n  附带三层防风控重试（致命熔断/限流退避/超时重试）\n  支持 bind_tools() 方法进行工具调用\n\n【⚠️核心维护规则 / Agent 指令】\n1. 绝对兼容底线：入参和出参必须严格死守 LangChain `invoke` / `bind_tools` 的标准数据结构。严禁私自破坏这套“伪装皮囊”，否则下游所有 P 层的 Agent 调用都会集体宕机报错。\n2. 防风控与重试红线：内置的三层防风控（401/403号池熔断、429限流指数退避、超时重试）及 UA 伪装指纹（FAKE_HEADERS）是扛封号的血泪经验。修改时坚决不许删改休眠退避逻辑（time.sleep），防止把中转 API 和代理服务打崩溃。\n3. 推理链（Thinking）保留：针对 Claude 等模型返回的 `thinking` 思维链块，是理解 AI 决策过程的核弹级情报。在解析返回消息时，严禁将其当做废数据抹去，必须妥善挂载在 `thinking_content` 字段上放行给外层记录。\n4. Thinking 模式硬性约束：当模型名含 "thinking" 时，`temperature` 必须强制为 `1`、`max_tokens` 必须设为 `128000`——这是 Anthropic API 的硬性要求，不是可调参数。改了就炸，API 会直接拒绝请求。\n\n用法：\n  adapter = NativeLLMAdapter(model="claude-sonnet-4-6", base_url="http://127.0.0.1:8045")\n  result = adapter.invoke([SystemMessage(...), HumanMessage(...)])\n  # result.content == "模型返回的文本"\n\n  # 工具调用模式\n  bound = adapter.bind_tools([tool1, tool2])\n  result = bound.invoke(messages)\n  # result.tool_calls == [{"name": "...", "args": {...}, "id": "..."}]\n'

import os
import re
import json
import time
import random
import logging
import uuid
import ipaddress
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Why: FAKE_HEADERS 统一在 config.py 定义，避免分散维护
from config import FAKE_HEADERS

try:
    from pydantic import ConfigDict
except ImportError:
    ConfigDict = None

try:
    from langchain_core.runnables import RunnableSerializable
    from langchain_core.messages import AIMessage as LCMessage
    HAS_LANGCHAIN = True
except ImportError:
    RunnableSerializable = object
    LCMessage = None
    HAS_LANGCHAIN = False

logger = logging.getLogger(__name__)

_GEMINI_OPENAI_COMPAT_MODEL_MARKERS = (
    'gemini-3.1-pro-high',
    'gemini-3.1-pro-low',
    'gemini-3-pro-high',
    'gemini-3-pro-low',
    'gemini-pro-agent',
    'gemini-3-flash-agent',
)

_CLAUDE_OPENAI_COMPAT_MODEL_MARKERS = (
    'claude-opus-4-6',
    'claude-sonnet-4-6',
)

_PERMISSION_DENIED_MARKERS = (
    'permission_denied',
    'accessnotconfigured',
    'api has not been used',
    'has not been used in project',
    'cloud code private api',
    'service disabled',
    'api disabled',
    'consumer invalid',
)

_INPUT_TOO_LARGE_MARKERS = (
    'maximum context length',
    'context_length_exceeded',
    'context length exceeded',
    'request too large',
    'payload too large',
    'prompt is too long',
    'input is too long',
    'too many tokens',
    'token limit',
    'input token count',
    'maximum number of input tokens',
    'content too large',
)

_MODEL_NOT_FOUND_MARKERS = (
    'model_not_found',
    'model not found',
    'model does not exist',
    'unknown provider for model',
)


def validate_model_base_url(base_url: str) -> tuple[bool, bool, str]:
    try:
        parsed = urlparse(base_url)
        hostname = (parsed.hostname or '').strip().lower()
    except Exception:
        return False, False, 'invalid model endpoint URL'

    if parsed.scheme not in {'http', 'https'} or not hostname:
        return False, False, 'invalid model endpoint URL'

    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname == 'localhost'

    if parsed.scheme == 'http' and not is_loopback:
        return False, False, 'external model endpoints must use HTTPS'
    return True, is_loopback, ''


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _classify_provider_error(error: Exception) -> Dict[str, Any]:
    err_text = str(error or '')
    err_lower = err_text.lower()

    if _contains_any(err_lower, _PERMISSION_DENIED_MARKERS):
        return {'category': 'permission_denied', 'retryable': False}
    if _contains_any(err_lower, _INPUT_TOO_LARGE_MARKERS):
        return {'category': 'input_too_large', 'retryable': False}
    if _contains_any(err_lower, _MODEL_NOT_FOUND_MARKERS):
        return {'category': 'model_not_found', 'retryable': False}
    if 'auth_unavailable' in err_lower or 'no auth available' in err_lower:
        return {'category': 'auth_unavailable', 'retryable': True}
    if '401' in err_lower or '403' in err_lower or 'balance' in err_lower:
        return {'category': 'risk_control', 'retryable': True}
    if '429' in err_lower or 'resource_exhausted' in err_lower:
        return {'category': 'rate_limit', 'retryable': True}
    if 'timeout' in err_lower or 'timed out' in err_lower or 'deadline' in err_lower or '504' in err_lower:
        return {'category': 'timeout', 'retryable': True}
    if (
        'connection' in err_lower
        or 'ssl' in err_lower
        or 'proxy' in err_lower
        or 'eof' in err_lower
    ):
        return {'category': 'network_error', 'retryable': True}
    if (
        re.search(r'\b(?:500|502|503)\b', err_lower)
        or 'bad gateway' in err_lower
        or 'service unavailable' in err_lower
        or 'internal_server_error' in err_lower
        or 'server_error' in err_lower
    ):
        return {'category': 'server_error', 'retryable': True}
    return {'category': 'unknown_error', 'retryable': True}


def _with_http_error_detail(error: Exception) -> Exception:
    response = getattr(error, 'response', None)
    if response is None:
        return error

    detail = ''
    try:
        payload = response.json()
        error_payload = payload.get('error', {}) if isinstance(payload, dict) else {}
        if isinstance(error_payload, dict):
            detail = ' '.join(
                str(error_payload.get(key) or '').strip()
                for key in ('code', 'type', 'message')
                if error_payload.get(key)
            )
        elif error_payload:
            detail = str(error_payload)
    except Exception:
        detail = str(getattr(response, 'text', '') or '')

    detail = detail.strip()[:1000]
    if not detail:
        return error
    return RuntimeError(f'{error}; response_error={detail}')


def _should_use_openai_compat_proxy(model: str, base_url: str) -> bool:
    model_lower = (model or '').lower()
    base_url_lower = (base_url or '').rstrip('/').lower()
    uses_proxy_model = (
        any(marker in model_lower for marker in _GEMINI_OPENAI_COMPAT_MODEL_MARKERS)
        or any(marker in model_lower for marker in _CLAUDE_OPENAI_COMPAT_MODEL_MARKERS)
    )
    if not uses_proxy_model:
        return False
    return 'antigravity/v1' in base_url_lower or base_url_lower.endswith('/v1')


def _is_official_gemini_direct_model(model: str) -> bool:
    model_lower = (model or '').lower()
    if not model_lower.startswith('gemini-'):
        return False
    if any(marker in model_lower for marker in _GEMINI_OPENAI_COMPAT_MODEL_MARKERS):
        return False
    return (
        model_lower.startswith('gemini-2.')
        or model_lower.startswith('gemini-3.5-')
        or model_lower.endswith('-preview')
        or model_lower.endswith('-latest')
    )


def _openai_compat_max_tokens(model: str) -> Optional[int]:
    override = os.environ.get('OPENAI_COMPAT_MAX_TOKENS', '').strip()
    if override:
        try:
            value = int(override)
            if value > 0:
                return value
        except ValueError:
            logger.warning("OPENAI_COMPAT_MAX_TOKENS is not a positive integer: %s", override)

    return None


def _env_flag(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name, '').strip().lower()
    if not raw_value:
        return default
    return raw_value in {'1', 'true', 'yes', 'on'}


def _env_positive_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name, '').strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        logger.warning("%s is not a positive integer: %s", name, raw_value)
        return default
    if value <= 0:
        logger.warning("%s must be positive: %s", name, raw_value)
        return default
    return value


def _grok_native_reasoning_effort() -> str:
    return (
        os.environ.get('GROK_NATIVE_REASONING_EFFORT')
        or os.environ.get('GROK_REASONING_EFFORT')
        or 'none'
    ).strip().lower()


def _collect_response_text(parts: List[str], value: Any) -> None:
    if isinstance(value, str):
        if value:
            parts.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_response_text(parts, item)
        return
    if isinstance(value, dict):
        for key in ('text', 'summary', 'content'):
            if key in value:
                _collect_response_text(parts, value.get(key))


def _extract_openai_responses_payload(data: Dict[str, Any]) -> tuple[str, str]:
    text_parts: List[str] = []
    reasoning_parts: List[str] = []

    output_text = data.get('output_text')
    if isinstance(output_text, str) and output_text:
        text_parts.append(output_text)

    for item in data.get('output') or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get('type')
        if item_type == 'reasoning':
            for key in ('summary', 'content', 'text'):
                if key in item:
                    _collect_response_text(reasoning_parts, item.get(key))
            continue
        if item_type == 'message' and not text_parts:
            _collect_response_text(text_parts, item.get('content'))

    return '\n'.join(text_parts).strip(), '\n'.join(reasoning_parts).strip()


def _openai_responses_incomplete_reason(data: Dict[str, Any]) -> str:
    details = data.get('incomplete_details') or {}
    if isinstance(details, dict):
        reason = details.get('reason')
        return str(reason or 'unknown')
    return str(details or 'unknown')


@dataclass
class FakeAIMessage:
    """模拟 LangChain 的 AIMessage，支持 .content / .tool_calls / .thinking_content"""
    content: str
    type: str = 'ai'
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    thinking_content: str = ''
    response_metadata: Dict[str, Any] = field(default_factory=dict)


def get_model_provenance(message: Any, default_model: str = '') -> Dict[str, Any]:
    model = str(default_model or '').strip()
    has_content = bool(str(getattr(message, 'content', '') or '').strip())
    defaults = {
        'requested_model': model,
        'attempted_models': [model] if model else [],
        'fallback_used': False,
        'fallback_model': '',
        'fallback_reason_category': '',
        'fallback_reason': '',
        'response_model': model if has_content else '',
        'effective_model': model if has_content else '',
        'adapter_provider': '',
        'final_error_category': '',
        'final_error': '',
    }
    metadata = getattr(message, 'response_metadata', {})
    if isinstance(metadata, dict):
        provenance = metadata.get('model_provenance')
        if isinstance(provenance, dict):
            defaults.update(provenance)
            requested_model = str(provenance.get('requested_model') or model)
            fallback_used = bool(provenance.get('fallback_used'))
            fallback_model = str(provenance.get('fallback_model') or '')

            attempted_models = provenance.get('attempted_models')
            if not isinstance(attempted_models, list):
                attempted_models = []
            attempted_models = [str(item) for item in attempted_models if str(item).strip()]
            if requested_model and requested_model not in attempted_models:
                attempted_models.insert(0, requested_model)
            if fallback_used and fallback_model and fallback_model not in attempted_models:
                attempted_models.append(fallback_model)

            response_model = str(provenance.get('response_model') or '')
            effective_model = str(provenance.get('effective_model') or '')
            if not effective_model:
                effective_model = response_model
            if not effective_model and has_content:
                effective_model = fallback_model if fallback_used and fallback_model else requested_model
            if not response_model:
                response_model = effective_model

            defaults.update(
                {
                    'requested_model': requested_model,
                    'attempted_models': attempted_models,
                    'fallback_used': fallback_used,
                    'fallback_model': fallback_model,
                    'response_model': response_model,
                    'effective_model': effective_model,
                }
            )
    return defaults

def _format_tool_schema(tool: Any) -> Dict[str, Any]:
    if hasattr(tool, 'name') and hasattr(tool, 'description'):
        schema = {
            'name': tool.name,
            'description': tool.description or ''
        }
        if hasattr(tool, 'args_schema') and tool.args_schema:
            try:
                schema['parameters'] = tool.args_schema.schema()
            except Exception:
                pass
        return schema
    if isinstance(tool, dict):
        return tool
    return {'name': str(tool), 'description': ''}

def _build_tools_prompt(tools: List[Any]) -> str:
    if not tools:
        return ''
    lines = [
        '\n\n## Available Tools\n',
        'You have the following tools available. To call a tool, respond with a JSON block in this EXACT format:\n',
        '```tool_call\n{"name": "tool_name", "arguments": {"param1": "value1"}}\n```\n',
        'You may call MULTIPLE tools by including multiple tool_call blocks.\n',
        'If you need to call a tool, respond ONLY with tool_call blocks and nothing else.\n',
        'If you do NOT need to call any tool, respond with your analysis directly.\n\n',
        '### Tool Definitions:\n'
    ]
    for tool in tools:
        schema = _format_tool_schema(tool)
        lines.append(f"**{schema['name']}**: {schema.get('description', '')}")
        if 'parameters' in schema:
            props = schema['parameters'].get('properties', {})
            if props:
                lines.append("  Parameters:")
                for pname, pinfo in props.items():
                    ptype = pinfo.get('type', 'string')
                    pdesc = pinfo.get('description', '')
                    lines.append(f"  - `{pname}` ({ptype}): {pdesc}")
        lines.append("")
    return "\n".join(lines)

def _parse_tool_calls(text: str) -> List[Dict[str, Any]]:
    tool_calls = []
    pattern = r'```tool_call\s*\n?(.*?)\n?```'
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        try:
            data = json.loads(match.strip())
            tool_call = {
                'name': data.get('name', ''),
                'args': data.get('arguments', data.get('args', {})),
                'id': f"call_{uuid.uuid4().hex[:8]}",
                'type': 'tool_call'
            }
            tool_calls.append(tool_call)
        except json.JSONDecodeError:
            logger.warning(f"无法解析 tool_call JSON: {match[:100]}")

    if not tool_calls:
        json_pattern = r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"arguments"\s*:\s*(\{[^}]*\})\s*\}'
        json_matches = re.findall(json_pattern, text, re.DOTALL)
        for name, args_str in json_matches:
            try:
                args = json.loads(args_str)
                tool_call = {
                    'name': name,
                    'args': args,
                    'id': f"call_{uuid.uuid4().hex[:8]}",
                    'type': 'tool_call'
                }
                tool_calls.append(tool_call)
            except json.JSONDecodeError as e:
                logger.warning(f"无法解析后备提取的 tool_call JSON: {args_str[:100]}... Error: {e}")

    return tool_calls

class NativeLLMAdapter(RunnableSerializable):
    """
    原生 SDK 适配器 — 继承 LangChain Runnable，可直接用在 prompt | llm 管道中

    支持的模型：
      - claude-*   → Anthropic 原生 SDK
      - gemini-*   → Google GenAI 原生 SDK
      - 其他       → 回退到 OpenAI 兼容协议

    支持 bind_tools() 和 prompt | llm 管道
    """
    model: str = ''
    base_url: str = ''
    api_key: str = ''
    max_retries: int = 3
    timeout: float = 300.0
    provider: str = 'openai_compat'
    # Why: 统一用空字典默认值，防止 bind_tools() 中 **self.extra_kwargs 解包崩溃
    #       原先 `field(default_factory=dict) if not HAS_LANGCHAIN else None` 在
    #       HAS_LANGCHAIN=True 时 extra_kwargs 默认为 None，类型与 Dict 声明矛盾
    extra_kwargs: Dict[str, Any] = {}
    last_error_category: str = ''
    last_error_message: str = ''
    last_response_model: str = ''
    _bound_tools: List[Any] = None
    _tools_prompt: str = ''

    if ConfigDict:
        model_config = ConfigDict(arbitrary_types_allowed=True)
    else:
        class Config:
            """Pydantic 配置，允许任意类型字段"""
            arbitrary_types_allowed = True

    def __init__(
        self,
        model: str = '',
        base_url: str = None,
        api_key: str = None,
        max_retries: int = 3,
        timeout: float = 300.0,
        **kwargs
    ):
        resolved_base_url = base_url or os.environ.get('ANTIGRAVITY_BASE_URL', 'http://127.0.0.1:8045')
        resolved_api_key = api_key or os.environ.get('ANTIGRAVITY_API_KEY', os.environ.get('OPENAI_API_KEY', ''))
        endpoint_ok, _, endpoint_error = validate_model_base_url(resolved_base_url)
        if not endpoint_ok:
            raise ValueError(endpoint_error)

        model_lower = model.lower()
        if _should_use_openai_compat_proxy(model_lower, resolved_base_url):
            provider = 'openai_compat'
        elif 'claude' in model_lower or 'opus' in model_lower:
            provider = 'anthropic'
        elif 'gemini' in model_lower:
            provider = 'google'
        else:
            provider = 'openai_compat'

        if HAS_LANGCHAIN:
            super().__init__(
                model=model,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                max_retries=max_retries,
                timeout=timeout,
                provider=provider,
                extra_kwargs=kwargs
            )
        else:
            super().__init__()
            self.model = model
            self.base_url = resolved_base_url
            self.api_key = resolved_api_key
            self.max_retries = max_retries
            self.timeout = timeout
            self.provider = provider
            self.extra_kwargs = kwargs

        self._bound_tools = []
        self._tools_prompt = ''

    @property
    def InputType(self) -> Any:
        return Any

    @property
    def OutputType(self) -> Any:
        return Any

    def invoke(self, messages: Any, config: Any = None) -> Any:
        system_prompt, user_prompt = self._parse_messages(messages)
        force_fallback = bool(config.get('force_fallback')) if isinstance(config, dict) else False
        forced_fallback_reason = (
            str(config.get('fallback_reason') or 'previous response failed semantic validation')
            if isinstance(config, dict)
            else 'previous response failed semantic validation'
        )
        self._reset_last_error()
        self.last_response_model = ''
        call_deadline = self._new_call_deadline()

        if self._tools_prompt:
            system_prompt = system_prompt + self._tools_prompt

        thinking_content = ''
        original_model = self.model
        attempted_models = [] if force_fallback and original_model == 'gpt-5.6-sol' else [original_model]
        selected_model = original_model
        fallback_used = False
        fallback_model = ''
        fallback_reason_category = ''
        fallback_reason = ''
        fallback_provider = ''
        fallback_endpoint_independent = False

        if force_fallback and original_model == 'gpt-5.6-sol':
            text = ''
            self._set_last_error('semantic_validation_failed', forced_fallback_reason)
        elif self.provider == 'anthropic':
            result = self._call_anthropic(system_prompt, user_prompt, call_deadline)
            if isinstance(result, tuple):
                text, thinking_content = result
            else:
                text = result
        elif self.provider == 'google':
            text = self._call_gemini(f"{system_prompt}\n\n{user_prompt}", call_deadline)
        else:
            result = self._call_openai_compat(system_prompt, user_prompt, call_deadline)
            if isinstance(result, tuple):
                text, thinking_content = result
            else:
                text = result

        # Fallback logic for gpt-5.6-sol to claude-opus-4-6-thinking.
        if original_model == 'gpt-5.6-sol' and (not text or self.last_error_category):
            err_msg_lower = self.last_error_message.lower()
            is_fallback_eligible = (
                self.last_error_category in (
                    'auth_unavailable',
                    'risk_control',
                    'rate_limit',
                    'permission_denied',
                    'timeout',
                    'timeout_budget_exhausted',
                    'server_error',
                    'network_error',
                    'incomplete_response',
                    'empty_response',
                    'semantic_validation_failed',
                    'model_not_found',
                    'unknown_error',
                ) or
                any(kw in err_msg_lower for kw in ('balance', 'quota', 'credit', 'insufficient', '401', '403', '429', 'limit', 'exceeded'))
            )
            if is_fallback_eligible:
                fallback_used = True
                fallback_model = str(
                    (self.extra_kwargs or {}).get('fallback_model')
                    or 'claude-opus-4-6-thinking'
                )
                fallback_reason_category = self.last_error_category
                fallback_reason = self.last_error_message
                attempted_models.append(fallback_model)
                logger.warning(
                    f"⚠️ [LLM Fallback] Model {original_model} failed (category={self.last_error_category}, msg={self.last_error_message}). "
                    f"Attempting fallback to backup model '{fallback_model}'..."
                )
                self._reset_last_error()
                self.last_response_model = ''
                selected_model = fallback_model
                fallback_base_url = str(
                    (self.extra_kwargs or {}).get('fallback_base_url') or ''
                ).strip()
                fallback_api_key = str(
                    (self.extra_kwargs or {}).get('fallback_api_key') or ''
                ).strip()
                if fallback_base_url and fallback_api_key:
                    try:
                        fallback_timeout = float(
                            (self.extra_kwargs or {}).get(
                                'fallback_request_timeout_seconds',
                                self.timeout,
                            )
                        )
                    except (TypeError, ValueError):
                        fallback_timeout = float(self.timeout)
                    fallback_kwargs: Dict[str, Any] = {}
                    fallback_total_timeout = (self.extra_kwargs or {}).get(
                        'fallback_total_timeout_seconds'
                    )
                    if fallback_total_timeout is not None:
                        fallback_kwargs['total_timeout_seconds'] = fallback_total_timeout
                    external_fallback = NativeLLMAdapter(
                        model=fallback_model,
                        base_url=fallback_base_url,
                        api_key=fallback_api_key,
                        max_retries=self.max_retries,
                        timeout=fallback_timeout,
                        **fallback_kwargs,
                    )
                    fallback_messages = []
                    if system_prompt:
                        fallback_messages.append(('system', system_prompt))
                    fallback_messages.append(('human', user_prompt))
                    fallback_response = external_fallback.invoke(fallback_messages)
                    text = str(getattr(fallback_response, 'content', '') or '')
                    thinking_content = str(
                        getattr(fallback_response, 'thinking_content', '') or ''
                    )
                    fallback_provider = external_fallback.provider
                    fallback_endpoint_independent = (
                        fallback_base_url.rstrip('/') != self.base_url.rstrip('/')
                        or fallback_api_key != self.api_key
                    )
                    self.last_response_model = external_fallback.last_response_model
                    if text.strip():
                        self._reset_last_error()
                    else:
                        self._set_last_error(
                            external_fallback.last_error_category or 'empty_response',
                            external_fallback.last_error_message
                            or 'fallback provider returned no output text',
                        )
                else:
                    object.__setattr__(self, 'model', fallback_model)
                    fallback_deadline = call_deadline
                    if (self.extra_kwargs or {}).get('fallback_total_timeout_seconds') is not None:
                        fallback_deadline = self._new_call_deadline('fallback_total_timeout_seconds')
                    try:
                        if self.provider == 'anthropic':
                            result = self._call_anthropic(system_prompt, user_prompt, fallback_deadline)
                            if isinstance(result, tuple):
                                text, thinking_content = result
                            else:
                                text = result
                        elif self.provider == 'google':
                            text = self._call_gemini(
                                f"{system_prompt}\n\n{user_prompt}",
                                fallback_deadline,
                            )
                        else:
                            result = self._call_openai_compat(system_prompt, user_prompt, fallback_deadline)
                            if isinstance(result, tuple):
                                text, thinking_content = result
                            else:
                                text = result
                    finally:
                        object.__setattr__(self, 'model', original_model)
                    fallback_provider = self.provider
        effective_model = self.last_response_model or (selected_model if text else '')
        model_provenance = {
            'requested_model': original_model,
            'attempted_models': attempted_models,
            'fallback_used': fallback_used,
            'fallback_model': fallback_model,
            'fallback_reason_category': fallback_reason_category,
            'fallback_reason': fallback_reason[:2000],
            'response_model': effective_model,
            'effective_model': effective_model,
            'adapter_provider': self.provider,
            'fallback_provider': fallback_provider,
            'fallback_endpoint_independent': fallback_endpoint_independent,
            'final_error_category': self.last_error_category,
            'final_error': self.last_error_message[:2000],
        }
        response_metadata = {'model_provenance': model_provenance}

        tool_calls = []
        if self._bound_tools:
            tool_calls = _parse_tool_calls(text)

        if tool_calls and HAS_LANGCHAIN and LCMessage:
            msg = LCMessage(content='', tool_calls=tool_calls, response_metadata=response_metadata)
            return msg
        elif tool_calls:
            return FakeAIMessage(content='', tool_calls=tool_calls, response_metadata=response_metadata)

        if HAS_LANGCHAIN and LCMessage:
            msg = LCMessage(content=text, response_metadata=response_metadata)
            msg.thinking_content = thinking_content
            return msg
        else:
            return FakeAIMessage(
                content=text,
                tool_calls=[],
                thinking_content=thinking_content,
                response_metadata=response_metadata,
            )

    def bind_tools(self, tools: List[Any]) -> 'NativeLLMAdapter':
        bound = NativeLLMAdapter(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            max_retries=self.max_retries,
            timeout=self.timeout,
            **(self.extra_kwargs or {})
        )
        bound.provider = self.provider
        bound._bound_tools = list(tools)
        bound._tools_prompt = _build_tools_prompt(tools)
        return bound

    def bind(self, **kwargs):
        return self

    def with_config(self, **kwargs):
        return self

    def _parse_messages(self, messages: Any) -> tuple[str, str]:
        if isinstance(messages, str):
            return '', messages

        system_parts = []
        user_parts = []

        if not isinstance(messages, (list, tuple)):
            return '', str(messages)

        for msg in messages:
            if isinstance(msg, tuple) and len(msg) == 2:
                role, content = msg
                if role in ('system',):
                    system_parts.append(content)
                    continue
                elif role in ('human', 'user'):
                    user_parts.append(content)
                    continue
                elif role in ('ai', 'assistant'):
                    user_parts.append(f"[Assistant]: {content}")
                    continue
                else:
                    user_parts.append(content)
                    continue

            if hasattr(msg, 'type') and hasattr(msg, 'content'):
                if msg.type == 'system':
                    system_parts.append(msg.content)
                    continue
                elif msg.type == 'human':
                    user_parts.append(msg.content)
                    continue
                elif msg.type == 'ai':
                    if hasattr(msg, 'tool_calls') and msg.tool_calls:
                        tc_str = json.dumps(msg.tool_calls, ensure_ascii=False)
                        user_parts.append(f"[Assistant tool calls]: {tc_str}")
                        continue
                    if msg.content:
                        user_parts.append(f"[Assistant]: {msg.content}")
                    continue
                elif msg.type == 'tool':
                    tool_name = getattr(msg, 'name', 'unknown_tool')
                    user_parts.append(f"[Tool Result - {tool_name}]: {msg.content}")
                    continue
                else:
                    if hasattr(msg, 'content'):
                        user_parts.append(str(msg.content))
                    else:
                        user_parts.append(str(msg))
                    continue

            if isinstance(msg, dict):
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                if role == 'system':
                    system_parts.append(content)
                    continue
                elif role == 'tool':
                    tool_name = msg.get('name', 'unknown_tool')
                    user_parts.append(f"[Tool Result - {tool_name}]: {content}")
                    continue
                else:
                    user_parts.append(content)
                    continue

            user_parts.append(str(msg))

        return '\n'.join(system_parts), '\n'.join(user_parts)

    def _reset_last_error(self) -> None:
        self.last_error_category = ''
        self.last_error_message = ''

    def _set_last_error(self, category: str, message: str) -> None:
        self.last_error_category = category
        self.last_error_message = message[:2000]

    def _new_call_deadline(self, budget_key: str = 'total_timeout_seconds') -> Optional[float]:
        raw_budget = (self.extra_kwargs or {}).get(budget_key)
        try:
            budget = float(raw_budget)
        except (TypeError, ValueError):
            return None
        if budget <= 0:
            return None
        return time.monotonic() + budget

    def _bounded_request_timeout(self, deadline: Optional[float]) -> float:
        if deadline is None:
            return float(self.timeout)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        return min(float(self.timeout), remaining)

    def _handle_provider_error(
        self,
        provider_label: str,
        error: Exception,
        attempt: int,
        *,
        risk_hint: str = '',
        deadline: Optional[float] = None,
    ) -> bool:
        error_info = _classify_provider_error(error)
        self._set_last_error(error_info['category'], str(error))
        wait = min(15 * (2 ** attempt), 120) + random.uniform(0, 2.0)
        if attempt >= self.max_retries - 1:
            logger.error(f"[{provider_label}] final attempt failed: {error}")
            return False

        raw_failover_categories = (self.extra_kwargs or {}).get('failover_categories', ())
        if isinstance(raw_failover_categories, str):
            failover_categories = {
                item.strip() for item in raw_failover_categories.split(',') if item.strip()
            }
        else:
            failover_categories = {
                str(item).strip()
                for item in (raw_failover_categories or ())
                if str(item).strip()
            }
        if error_info['category'] in failover_categories:
            logger.warning(
                '[%s] category=%s configured for immediate failover; stop primary retries',
                provider_label,
                error_info['category'],
            )
            return False

        if deadline is not None:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= wait:
                category_label = {
                    'auth_unavailable': '认证池暂时不可用',
                    'rate_limit': '触发限流',
                    'timeout': '请求超时',
                }.get(error_info['category'], error_info['category'])
                logger.warning(
                    '[%s] %s；总调用预算仅剩 %.1fs，不再等待 %.1fs 重试',
                    provider_label,
                    category_label,
                    remaining,
                    wait,
                )
                return False

        if error_info['category'] == 'permission_denied':
            logger.error(f"🚫 [{provider_label}] 权限/项目配置错误，停止重试: {error}")
            return False
        if error_info['category'] == 'input_too_large':
            logger.error(f"📏 [{provider_label}] 请求过长或上下文超限，停止重试: {error}")
            return False
        if not error_info['retryable']:
            logger.error(
                f"🚫 [{provider_label}] 不可重试错误(category={error_info['category']})，停止重试: {error}"
            )
            return False
        if error_info['category'] == 'auth_unavailable':
            logger.warning(
                f"⚠️ [{provider_label}] 认证池暂时不可用，等待 {wait:.1f}s（{attempt + 1}/{self.max_retries}）"
            )
            time.sleep(wait)
            return True
        if error_info['category'] == 'risk_control':
            hint_text = f' {risk_hint}' if risk_hint else ''
            logger.warning(
                f"⚠️ [{provider_label}] 账号风控(401/403)，等待 {wait:.1f}s{hint_text}（{attempt + 1}/{self.max_retries}）"
            )
            time.sleep(wait)
            return True
        if error_info['category'] == 'rate_limit':
            logger.warning(f"⏳ [{provider_label}] 触发限流，等待 {wait:.1f}s（{attempt + 1}/{self.max_retries}）")
            time.sleep(wait)
            return True
        if error_info['category'] == 'timeout':
            logger.warning(f"⏳ [{provider_label}] 超时，等待 {wait:.1f}s（{attempt + 1}/{self.max_retries}）")
            time.sleep(wait)
            return True
        if error_info['category'] == 'network_error':
            logger.error(f"❌ [{provider_label}] 网络连接异常，等待 {wait:.1f}s（{attempt + 1}/{self.max_retries}）: {error}")
            time.sleep(wait)
            return True

        logger.warning(f"⚠️ [{provider_label}] 异常: {error}，等待 {wait:.1f}s（{attempt + 1}/{self.max_retries}）")
        time.sleep(wait)
        return True

    def _call_anthropic(
        self,
        system_prompt: str,
        user_prompt: str,
        deadline: Optional[float] = None,
    ) -> tuple[str, str]:
        if getattr(self, '_anthropic_client', None) is None:
            from anthropic import Anthropic
            client = Anthropic(api_key=self.api_key, base_url=self.base_url, default_headers=FAKE_HEADERS)
            object.__setattr__(self, '_anthropic_client', client)
        client = self._anthropic_client

        kwargs = {
            'model': self.model,
            'max_tokens': 16000,
            'timeout': self.timeout
        }

        if system_prompt:
            kwargs['system'] = system_prompt

        kwargs['messages'] = [{'role': 'user', 'content': user_prompt}]

        if 'thinking' in self.model.lower():
            kwargs['temperature'] = 1
            kwargs['max_tokens'] = 128000

        for attempt in range(self.max_retries):
            try:
                request_timeout = self._bounded_request_timeout(deadline)
                if request_timeout <= 0:
                    if not self.last_error_category:
                        self._set_last_error(
                            'timeout_budget_exhausted',
                            'total call timeout budget exhausted',
                        )
                    break
                kwargs['timeout'] = request_timeout
                response = client.messages.create(**kwargs)
                self.last_response_model = str(getattr(response, 'model', '') or self.model)
                text_blocks = [b.text for b in response.content if b.type == 'text']
                thinking_blocks = [b.thinking for b in response.content if b.type == 'thinking']

                text = text_blocks[0] if text_blocks else ''
                thinking = '\n\n'.join(thinking_blocks) if thinking_blocks else ''

                if text.strip():
                    self._reset_last_error()
                else:
                    self._set_last_error('empty_response', 'provider returned HTTP 200 without output text')

                if thinking:
                    logger.info(f"🧠 [Anthropic] 捕获 thinking 推理链: {len(thinking)} 字符")

                return text, thinking
            except Exception as e:
                if not self._handle_provider_error(
                    'Anthropic',
                    e,
                    attempt,
                    risk_hint='让反代切换账号',
                    deadline=deadline,
                ):
                    break

        logger.error(f"❌ [Anthropic] 调用失败，最后错误分类: {self.last_error_category or 'unknown_error'}")
        return '', ''

    def _call_gemini(self, prompt: str, deadline: Optional[float] = None) -> str:
        # 统一使用最新 google.genai 架构（同时处理官方直连与旧 3.x 反代）
        if getattr(self, '_gemini_client', None) is None:
            from google import genai
            from google.genai import types

            timeout_ms = max(1, int(self.timeout * 1000))
            
            if _is_official_gemini_direct_model(self.model):
                # 官方 Gemini 模型走默认 URL，避免把 3.5 误送进本地 3.1 反代。
                client = genai.Client(
                    api_key=self.api_key,
                    http_options=types.HttpOptions(timeout=timeout_ms),
                )
            else:
                # 旧版 3.x 兼容模型走反代逻辑 (通过 http_options.base_url 挂载)
                client = genai.Client(
                    api_key=self.api_key,
                    http_options=types.HttpOptions(base_url=self.base_url, timeout=timeout_ms)
                )
            object.__setattr__(self, '_gemini_client', client)
        
        client = self._gemini_client

        # Why: import 提出循环外，避免每次重试都做模块查找（虽然 Python 有缓存，但无必要）
        from google.genai import types

        for attempt in range(self.max_retries):
            try:
                if self._bounded_request_timeout(deadline) <= 0:
                    if not self.last_error_category:
                        self._set_last_error(
                            'timeout_budget_exhausted',
                            'total call timeout budget exhausted',
                        )
                    break
                kwargs = {
                    "model": self.model,
                    "contents": prompt,
                    "config": types.GenerateContentConfig(
                        temperature=0.5,
                        max_output_tokens=16000
                    )
                }
                
                # Gemini 2.5 强制挂载原生搜索
                if '2.5' in self.model:
                    kwargs["config"].tools = [types.Tool(google_search=types.GoogleSearch())]
                
                response = client.models.generate_content(**kwargs)
                self.last_response_model = str(
                    getattr(response, 'model_version', '')
                    or getattr(response, 'model', '')
                    or self.model
                )
                text = response.text if response.text else ''
                if text.strip():
                    self._reset_last_error()
                else:
                    self._set_last_error('empty_response', 'provider returned HTTP 200 without output text')
                return text
            except Exception as e:
                if not self._handle_provider_error(
                    'Gemini SDK',
                    e,
                    attempt,
                    deadline=deadline,
                ):
                    break

        logger.error(f"❌ [Gemini SDK] 调用失败，最后错误分类: {self.last_error_category or 'unknown_error'}")
        return ''

    def _call_grok_native(self, system_prompt: str, user_prompt: str) -> str:
        import requests

        base = (self.base_url or 'https://api.x.ai').rstrip('/')
        if base.endswith('/v1'):
            url = f"{base}/responses"
        else:
            url = f"{base}/v1/responses"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        input_messages = []
        if system_prompt:
            input_messages.append({"role": "system", "content": system_prompt})
        input_messages.append({"role": "user", "content": user_prompt})

        payload = {
            "model": self.model,
            "input": input_messages,
            "max_output_tokens": _env_positive_int("GROK_NATIVE_MAX_OUTPUT_TOKENS", 4096),
        }
        reasoning_effort = _grok_native_reasoning_effort()
        if reasoning_effort not in {"", "0", "false", "off", "disabled"}:
            payload["reasoning"] = {"effort": reasoning_effort}
        if _env_flag("GROK_NATIVE_ENABLE_X_SEARCH", default=False):
            payload["tools"] = [{"type": "x_search"}]

        for attempt in range(self.max_retries):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                self.last_response_model = str(data.get('model') or self.model)

                text = ""
                if "output" in data:
                    for block in data["output"]:
                        if isinstance(block, str):
                            text += block + "\n"
                            continue
                        if not isinstance(block, dict):
                            continue
                        if "text" in block:
                            text += str(block["text"]) + "\n"
                        content_blocks = block.get("content")
                        if isinstance(content_blocks, str):
                            text += content_blocks + "\n"
                        elif isinstance(content_blocks, list):
                            for content_block in content_blocks:
                                if isinstance(content_block, str):
                                    text += content_block + "\n"
                                elif isinstance(content_block, dict) and "text" in content_block:
                                    text += str(content_block.get("text") or "") + "\n"
                text = text.strip()
                if text:
                    self._reset_last_error()
                else:
                    self._set_last_error('empty_response', 'provider returned HTTP 200 without output text')
                return text
            except Exception as e:
                if not self._handle_provider_error('Grok Native', e, attempt):
                    break
                continue
        logger.error(f"❌ [Grok Native] 调用失败，最后错误分类: {self.last_error_category or 'unknown_error'}")
        return ""

    def _call_openai_compat(
        self,
        system_prompt: str,
        user_prompt: str,
        deadline: Optional[float] = None,
    ) -> tuple[str, str]:
        # 如果是 Grok，挂载特种 Responses API 来支持原生 X_search
        if 'grok' in self.model.lower():
            return self._call_grok_native(system_prompt, user_prompt), ''
        if (self.extra_kwargs or {}).get('use_responses_api'):
            return self._call_openai_responses(system_prompt, user_prompt, deadline)

        # Why: 惰性缓存 OpenAI Client，与 _call_anthropic/_call_gemini 保持一致，避免每次调用都新建连接
        if getattr(self, '_openai_client', None) is None:
            from openai import OpenAI
            base_url = self.base_url + '/v1' if not self.base_url.endswith('/v1') else self.base_url
            # Keep retry budgeting in this adapter instead of the SDK's hidden retry loop.
            client = OpenAI(api_key=self.api_key, base_url=base_url, max_retries=0)
            object.__setattr__(self, '_openai_client', client)
        client = self._openai_client

        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': user_prompt})

        for attempt in range(self.max_retries):
            try:
                request_timeout = self._bounded_request_timeout(deadline)
                if request_timeout <= 0:
                    if not self.last_error_category:
                        self._set_last_error(
                            'timeout_budget_exhausted',
                            'total call timeout budget exhausted',
                        )
                    logger.error('[OpenAI兼容] 总调用预算已耗尽，停止请求')
                    break
                kwargs = {
                    'model': self.model,
                    'messages': messages,
                    'timeout': request_timeout,
                }
                max_tokens = _openai_compat_max_tokens(self.model)
                if max_tokens is not None:
                    kwargs['max_tokens'] = max_tokens
                reasoning_effort = (self.extra_kwargs or {}).get('reasoning_effort')
                if reasoning_effort:
                    kwargs['reasoning_effort'] = reasoning_effort
                response = client.chat.completions.create(**kwargs)
                self.last_response_model = str(getattr(response, 'model', '') or self.model)
                message = response.choices[0].message
                text = message.content or ''
                reasoning = getattr(message, 'reasoning_content', '') or ''

                if text.strip():
                    self._reset_last_error()
                else:
                    self._set_last_error('empty_response', 'provider returned HTTP 200 without output text')

                if reasoning:
                    logger.info(f"🧠 [OpenAI兼容] 捕获 reasoning_content 推理链: {len(reasoning)} 字符")

                return text, reasoning
            except Exception as e:
                if not self._handle_provider_error(
                    'OpenAI兼容',
                    e,
                    attempt,
                    deadline=deadline,
                ):
                    break

        logger.error(f"❌ [OpenAI兼容] 调用失败，最后错误分类: {self.last_error_category or 'unknown_error'}")
        return '', ''

    def _call_openai_responses(
        self,
        system_prompt: str,
        user_prompt: str,
        deadline: Optional[float] = None,
    ) -> tuple[str, str]:
        import requests

        if deadline is None:
            deadline = self._new_call_deadline()

        base_url = self.base_url + '/v1' if not self.base_url.endswith('/v1') else self.base_url
        url = f"{base_url}/responses"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload: Dict[str, Any] = {
            "model": self.model,
            "input": user_prompt,
        }
        if system_prompt:
            payload["instructions"] = system_prompt
        extra_kwargs = self.extra_kwargs or {}
        reasoning: Dict[str, Any] = {}
        reasoning_effort = extra_kwargs.get('reasoning_effort')
        if reasoning_effort:
            reasoning["effort"] = reasoning_effort
        reasoning_summary = extra_kwargs.get('reasoning_summary')
        if reasoning_summary:
            reasoning["summary"] = reasoning_summary
        if reasoning:
            payload["reasoning"] = reasoning
        max_tokens = _openai_compat_max_tokens(self.model)
        if max_tokens is not None:
            payload["max_output_tokens"] = max_tokens

        for attempt in range(self.max_retries):
            try:
                request_timeout = self._bounded_request_timeout(deadline)
                if request_timeout <= 0:
                    if not self.last_error_category:
                        self._set_last_error('timeout_budget_exhausted', 'total call timeout budget exhausted')
                    logger.error('[OpenAI Responses] 总调用预算已耗尽，停止请求')
                    break
                response = requests.post(url, headers=headers, json=payload, timeout=request_timeout)
                response.raise_for_status()
                data = response.json()
                self.last_response_model = str(data.get('model') or self.model)
                text, reasoning = _extract_openai_responses_payload(data)

                if reasoning:
                    logger.info(f"🧠 [OpenAI Responses] 捕获 reasoning 推理链: {len(reasoning)} 字符")

                if data.get('status') == 'incomplete':
                    reason = _openai_responses_incomplete_reason(data)
                    self._set_last_error('incomplete_response', reason)
                    logger.warning(
                        "[OpenAI Responses] 响应未完成: reason=%s, partial_text_chars=%s, reasoning_chars=%s",
                        reason,
                        len(text),
                        len(reasoning),
                    )
                    if attempt < self.max_retries - 1:
                        wait = min(15 * (2 ** attempt), 120) + random.uniform(0, 2.0)
                        if deadline is not None:
                            remaining = max(0.0, deadline - time.monotonic())
                            if remaining <= wait:
                                self._set_last_error(
                                    'timeout_budget_exhausted',
                                    'total call timeout budget exhausted after incomplete response',
                                )
                                logger.warning(
                                    '[OpenAI Responses] 剩余预算 %.1fs 不足退避 %.1fs，切换备胎',
                                    remaining,
                                    wait,
                                )
                                break
                        logger.warning(
                            "[OpenAI Responses] 等待 %.1fs 后重试未完成响应（%s/%s）",
                            wait,
                            attempt + 1,
                            self.max_retries,
                        )
                        time.sleep(wait)
                        continue

                    return text, reasoning

                if not text.strip():
                    self._set_last_error('empty_response', 'provider returned HTTP 200 without output text')
                    return text, reasoning
                if self.last_error_category:
                    self._reset_last_error()
                return text, reasoning
            except Exception as e:
                provider_error = _with_http_error_detail(e)
                if not self._handle_provider_error(
                    'OpenAI Responses',
                    provider_error,
                    attempt,
                    deadline=deadline,
                ):
                    break

        logger.error(f"❌ [OpenAI Responses] 调用失败，最后错误分类: {self.last_error_category or 'unknown_error'}")
        return '', ''

    def __repr__(self):
        return f"NativeLLMAdapter(model={self.model}, provider={self.provider})"
