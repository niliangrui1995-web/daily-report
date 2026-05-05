__doc__ = '\n原生 SDK 适配器 — 伪装成 LangChain ChatOpenAI\n\n核心原理：\n  外部接口完全兼容 LangChain 的 .invoke(messages) 方法\n  内部根据模型名自动选择 Anthropic SDK 或 Google GenAI SDK 直连\n  附带三层防风控重试（致命熔断/限流退避/超时重试）\n  支持 bind_tools() 方法进行工具调用\n\n【⚠️核心维护规则 / Agent 指令】\n1. 绝对兼容底线：入参和出参必须严格死守 LangChain `invoke` / `bind_tools` 的标准数据结构。严禁私自破坏这套“伪装皮囊”，否则下游所有 P 层的 Agent 调用都会集体宕机报错。\n2. 防风控与重试红线：内置的三层防风控（401/403号池熔断、429限流指数退避、超时重试）及 UA 伪装指纹（FAKE_HEADERS）是扛封号的血泪经验。修改时坚决不许删改休眠退避逻辑（time.sleep），防止把中转 API 和代理服务打崩溃。\n3. 推理链（Thinking）保留：针对 Claude 等模型返回的 `thinking` 思维链块，是理解 AI 决策过程的核弹级情报。在解析返回消息时，严禁将其当做废数据抹去，必须妥善挂载在 `thinking_content` 字段上放行给外层记录。\n4. Thinking 模式硬性约束：当模型名含 "thinking" 时，`temperature` 必须强制为 `1`、`max_tokens` 必须设为 `128000`——这是 Anthropic API 的硬性要求，不是可调参数。改了就炸，API 会直接拒绝请求。\n\n用法：\n  adapter = NativeLLMAdapter(model="claude-sonnet-4-6", base_url="http://127.0.0.1:8045")\n  result = adapter.invoke([SystemMessage(...), HumanMessage(...)])\n  # result.content == "模型返回的文本"\n\n  # 工具调用模式\n  bound = adapter.bind_tools([tool1, tool2])\n  result = bound.invoke(messages)\n  # result.tool_calls == [{"name": "...", "args": {...}, "id": "..."}]\n'

import os
import re
import json
import time
import random
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List

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


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _classify_provider_error(error: Exception) -> Dict[str, Any]:
    err_text = str(error or '')
    err_lower = err_text.lower()

    if _contains_any(err_lower, _PERMISSION_DENIED_MARKERS):
        return {'category': 'permission_denied', 'retryable': False}
    if _contains_any(err_lower, _INPUT_TOO_LARGE_MARKERS):
        return {'category': 'input_too_large', 'retryable': False}
    if '401' in err_lower or '403' in err_lower or 'balance' in err_lower:
        return {'category': 'risk_control', 'retryable': True}
    if '429' in err_lower or '503' in err_lower or 'resource_exhausted' in err_lower:
        return {'category': 'rate_limit', 'retryable': True}
    if 'timeout' in err_lower or 'deadline' in err_lower:
        return {'category': 'timeout', 'retryable': True}
    if 'connection' in err_lower or 'ssl' in err_lower or 'proxy' in err_lower:
        return {'category': 'network_error', 'retryable': True}
    return {'category': 'unknown_error', 'retryable': True}


def _should_use_openai_compat_proxy(model: str, base_url: str) -> bool:
    model_lower = (model or '').lower()
    base_url_lower = (base_url or '').rstrip('/').lower()
    if 'gemini-3.1-pro' not in model_lower and 'claude-opus-4-6' not in model_lower:
        return False
    return 'antigravity/v1' in base_url_lower or base_url_lower.endswith('/v1')

@dataclass
class FakeAIMessage:
    """模拟 LangChain 的 AIMessage，支持 .content / .tool_calls / .thinking_content"""
    content: str
    type: str = 'ai'
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    thinking_content: str = ''

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
        _ = config
        self._reset_last_error()

        if self._tools_prompt:
            system_prompt = system_prompt + self._tools_prompt

        thinking_content = ''

        if self.provider == 'anthropic':
            result = self._call_anthropic(system_prompt, user_prompt)
            if isinstance(result, tuple):
                text, thinking_content = result
            else:
                text = result
        elif self.provider == 'google':
            text = self._call_gemini(f"{system_prompt}\n\n{user_prompt}")
        else:
            result = self._call_openai_compat(system_prompt, user_prompt)
            if isinstance(result, tuple):
                text, thinking_content = result
            else:
                text = result

        tool_calls = []
        if self._bound_tools:
            tool_calls = _parse_tool_calls(text)

        if tool_calls and HAS_LANGCHAIN and LCMessage:
            msg = LCMessage(content='', tool_calls=tool_calls)
            return msg
        elif tool_calls:
            return FakeAIMessage(content='', tool_calls=tool_calls)

        if HAS_LANGCHAIN and LCMessage:
            msg = LCMessage(content=text)
            msg.thinking_content = thinking_content
            return msg
        else:
            return FakeAIMessage(content=text, tool_calls=[], thinking_content=thinking_content)

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

    def _handle_provider_error(
        self,
        provider_label: str,
        error: Exception,
        attempt: int,
        *,
        risk_hint: str = '',
    ) -> bool:
        error_info = _classify_provider_error(error)
        self._set_last_error(error_info['category'], str(error))
        wait = min(15 * (2 ** attempt), 120) + random.uniform(0, 2.0)

        if error_info['category'] == 'permission_denied':
            logger.error(f"🚫 [{provider_label}] 权限/项目配置错误，停止重试: {error}")
            return False
        if error_info['category'] == 'input_too_large':
            logger.error(f"📏 [{provider_label}] 请求过长或上下文超限，停止重试: {error}")
            return False
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

    def _call_anthropic(self, system_prompt: str, user_prompt: str) -> tuple[str, str]:
        if getattr(self, '_anthropic_client', None) is None:
            from anthropic import Anthropic
            client = Anthropic(api_key=self.api_key, base_url=self.base_url, default_headers=FAKE_HEADERS)
            object.__setattr__(self, '_anthropic_client', client)
        client = getattr(self, '_anthropic_client')

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
                response = client.messages.create(**kwargs)
                text_blocks = [b.text for b in response.content if b.type == 'text']
                thinking_blocks = [b.thinking for b in response.content if b.type == 'thinking']

                text = text_blocks[0] if text_blocks else ''
                thinking = '\n\n'.join(thinking_blocks) if thinking_blocks else ''

                if thinking:
                    logger.info(f"🧠 [Anthropic] 捕获 thinking 推理链: {len(thinking)} 字符")

                return text, thinking
            except Exception as e:
                if not self._handle_provider_error('Anthropic', e, attempt, risk_hint='让反代切换账号'):
                    break

        logger.error(f"❌ [Anthropic] 调用失败，最后错误分类: {self.last_error_category or 'unknown_error'}")
        return '', ''

    def _call_gemini(self, prompt: str) -> str:
        # 统一使用最新 google.genai 架构（同时处理 2.5 直连与 3.1 反代）
        if getattr(self, '_gemini_client', None) is None:
            from google import genai
            from google.genai import types
            
            if '2.5' in self.model:
                # 2.5 走官方默认 URL，原生直连通道 + 环境代理
                client = genai.Client(api_key=self.api_key)
            else:
                # 3.1 Pro 走反代逻辑 (通过 http_options.base_url 挂载)
                client = genai.Client(
                    api_key=self.api_key,
                    http_options=types.HttpOptions(base_url=self.base_url)
                )
            object.__setattr__(self, '_gemini_client', client)
        
        client = getattr(self, '_gemini_client')

        # Why: import 提出循环外，避免每次重试都做模块查找（虽然 Python 有缓存，但无必要）
        from google.genai import types

        for attempt in range(self.max_retries):
            try:
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
                return response.text if response.text else ''
            except Exception as e:
                if not self._handle_provider_error('Gemini SDK', e, attempt):
                    break

        logger.error(f"❌ [Gemini SDK] 调用失败，最后错误分类: {self.last_error_category or 'unknown_error'}")
        return ''

    def _call_grok_native(self, system_prompt: str, user_prompt: str) -> str:
        import requests

        url = "https://api.x.ai/v1/responses"
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
            "tools": [{"type": "x_search"}]
        }

        for attempt in range(self.max_retries):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()

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
                return text.strip()
            except Exception as e:
                wait = min(15 * (2 ** attempt), 120) + random.uniform(0, 2.0)
                logger.warning(f"⚠️ [Grok Native] 请求异常: {e}，等待 {wait:.1f}s（{attempt + 1}/{self.max_retries}）")
                time.sleep(wait)
        logger.error(f"❌ [Grok Native] {self.max_retries} 次重试全部失败")
        return ""

    def _call_openai_compat(self, system_prompt: str, user_prompt: str) -> tuple[str, str]:
        # 如果是 Grok，挂载特种 Responses API 来支持原生 X_search
        if 'grok' in self.model.lower():
            return self._call_grok_native(system_prompt, user_prompt), ''

        # Why: 惰性缓存 OpenAI Client，与 _call_anthropic/_call_gemini 保持一致，避免每次调用都新建连接
        if getattr(self, '_openai_client', None) is None:
            from openai import OpenAI
            base_url = self.base_url + '/v1' if not self.base_url.endswith('/v1') else self.base_url
            client = OpenAI(api_key=self.api_key, base_url=base_url)
            object.__setattr__(self, '_openai_client', client)
        client = getattr(self, '_openai_client')

        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': user_prompt})

        for attempt in range(self.max_retries):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=16000,
                    timeout=self.timeout
                )
                message = response.choices[0].message
                text = message.content or ''
                reasoning = getattr(message, 'reasoning_content', '') or ''

                if reasoning:
                    logger.info(f"🧠 [OpenAI兼容] 捕获 reasoning_content 推理链: {len(reasoning)} 字符")

                return text, reasoning
            except Exception as e:
                if not self._handle_provider_error('OpenAI兼容', e, attempt):
                    break

        logger.error(f"❌ [OpenAI兼容] 调用失败，最后错误分类: {self.last_error_category or 'unknown_error'}")
        return '', ''

    def __repr__(self):
        return f"NativeLLMAdapter(model={self.model}, provider={self.provider})"
