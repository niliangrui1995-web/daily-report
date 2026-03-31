# -*- coding: utf-8 -*-
"""
原生 SDK 适配器 — 伪装成 LangChain ChatOpenAI

核心原理：
  外部接口完全兼容 LangChain 的 .invoke(messages) 方法
  内部根据模型名自动选择 Anthropic SDK 或 Google GenAI SDK 直连
  附带三层防风控重试（致命熔断/限流退避/超时重试）
  支持 bind_tools() 方法进行工具调用

用法：
  adapter = NativeLLMAdapter(model="claude-sonnet-4-6", base_url="http://127.0.0.1:8045")
  result = adapter.invoke([SystemMessage(...), HumanMessage(...)])
  # result.content == "模型返回的文本"

  # 工具调用模式
  bound = adapter.bind_tools([tool1, tool2])
  result = bound.invoke(messages)
  # result.tool_calls == [{"name": "...", "args": {...}, "id": "..."}]
"""

import os
import re
import json
import time
import random
import logging
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

try:
    from langchain_core.runnables import RunnableSerializable
    from langchain_core.messages import AIMessage as LCMessage
    HAS_LANGCHAIN = True
except ImportError:
    # 兜底：如果没有 langchain_core，用 object
    RunnableSerializable = object
    LCMessage = None
    HAS_LANGCHAIN = False

logger = logging.getLogger(__name__)

# UA 伪装，防止反代检测到 Python SDK 指纹
FAKE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


@dataclass
class FakeToolCall:
    """模拟 LangChain 的 ToolCall"""
    name: str
    args: Dict[str, Any]
    id: str


@dataclass
class FakeAIMessage:
    """模拟 LangChain 的 AIMessage，支持 .content 和 .tool_calls"""
    content: str
    type: str = "ai"
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


def _format_tool_schema(tool: Any) -> Dict[str, Any]:
    """将 LangChain @tool 定义转为 JSON Schema 描述"""
    if hasattr(tool, "name") and hasattr(tool, "description"):
        # LangChain tool 对象
        schema = {
            "name": tool.name,
            "description": tool.description or "",
        }
        # 尝试提取参数 schema
        if hasattr(tool, "args_schema") and tool.args_schema:
            try:
                schema["parameters"] = tool.args_schema.schema()
            except Exception:
                pass
        return schema
    elif isinstance(tool, dict):
        return tool
    else:
        return {"name": str(tool), "description": ""}


def _build_tools_prompt(tools: List[Any]) -> str:
    """将工具列表转换为 prompt 注入文本"""
    if not tools:
        return ""

    lines = [
        "\n\n## Available Tools\n",
        "You have the following tools available. To call a tool, respond with a JSON block in this EXACT format:\n",
        '```tool_call\n{"name": "tool_name", "arguments": {"param1": "value1"}}\n```\n',
        "You may call MULTIPLE tools by including multiple tool_call blocks.\n",
        "If you need to call a tool, respond ONLY with tool_call blocks and nothing else.\n",
        "If you do NOT need to call any tool, respond with your analysis directly.\n\n",
        "### Tool Definitions:\n",
    ]

    for tool in tools:
        schema = _format_tool_schema(tool)
        lines.append(f"**{schema['name']}**: {schema.get('description', '')}")
        if "parameters" in schema:
            props = schema["parameters"].get("properties", {})
            if props:
                lines.append("  Parameters:")
                for pname, pinfo in props.items():
                    ptype = pinfo.get("type", "string")
                    pdesc = pinfo.get("description", "")
                    lines.append(f"  - `{pname}` ({ptype}): {pdesc}")
        lines.append("")

    return "\n".join(lines)


def _parse_tool_calls(text: str) -> List[Dict[str, Any]]:
    """从模型输出中解析 tool_call 块"""
    tool_calls = []

    # 匹配 ```tool_call ... ``` 块
    pattern = r"```tool_call\s*\n?(.*?)\n?```"
    matches = re.findall(pattern, text, re.DOTALL)

    for match in matches:
        try:
            data = json.loads(match.strip())
            tool_call = {
                "name": data.get("name", ""),
                "args": data.get("arguments", data.get("args", {})),
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "tool_call",
            }
            tool_calls.append(tool_call)
        except json.JSONDecodeError:
            logger.warning(f"无法解析 tool_call JSON: {match[:100]}")
            continue

    # 兜底：如果没有 ```tool_call``` 块，尝试匹配裸 JSON
    if not tool_calls:
        # 尝试匹配 {"name": "...", "arguments": {...}} 格式
        json_pattern = r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"arguments"\s*:\s*(\{[^}]*\})\s*\}'
        json_matches = re.findall(json_pattern, text, re.DOTALL)
        for name, args_str in json_matches:
            try:
                args = json.loads(args_str)
                tool_calls.append({
                    "name": name,
                    "args": args,
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "tool_call",
                })
            except json.JSONDecodeError:
                continue

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

    # Pydantic 字段声明
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    max_retries: int = 3
    timeout: float = 300.0
    provider: str = "openai_compat"
    extra_kwargs: Dict[str, Any] = {}
    _bound_tools: List[Any] = []
    _tools_prompt: str = ""

    class Config:
        """Pydantic 配置，允许任意类型字段"""
        arbitrary_types_allowed = True
        underscore_attrs_are_private = True

    def __init__(
        self,
        model: str = "",
        base_url: str = None,
        api_key: str = None,
        max_retries: int = 3,
        timeout: float = 300.0,
        **kwargs,
    ):
        # 先计算值
        resolved_base_url = base_url or os.environ.get("ANTIGRAVITY_BASE_URL", "http://127.0.0.1:8045")
        resolved_api_key = api_key or os.environ.get(
            "ANTIGRAVITY_API_KEY",
            os.environ.get("OPENAI_API_KEY", "")
        )

        # 判断模型类型
        model_lower = model.lower()
        if "claude" in model_lower or "opus" in model_lower:
            provider = "anthropic"
        elif "gemini" in model_lower:
            provider = "google"
        else:
            provider = "openai_compat"

        # 调用 Pydantic super().__init__
        super().__init__(
            model=model,
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            max_retries=max_retries,
            timeout=timeout,
            provider=provider,
            extra_kwargs=kwargs,
        )

        # 私有字段
        self._bound_tools = []
        self._tools_prompt = ""

    # ================================================================
    #  LangChain 兼容接口（Runnable 协议）
    # ================================================================

    @property
    def InputType(self) -> Any:
        return Any

    @property
    def OutputType(self) -> Any:
        return Any

    def invoke(self, messages: Any, config: Any = None, **kwargs) -> Any:
        """
        LangChain Runnable.invoke() 的实现

        接收 LangChain Message 列表或字符串，返回 AIMessage 或 FakeAIMessage
        如果绑定了工具，会解析工具调用
        """
        # 将 LangChain 消息转换为通用格式
        system_prompt, user_prompt = self._parse_messages(messages)

        # 如果绑定了工具，注入工具描述到 system prompt
        if self._tools_prompt:
            system_prompt = system_prompt + self._tools_prompt

        # 根据 provider 调用不同的 SDK
        if self.provider == "anthropic":
            text = self._call_anthropic(system_prompt, user_prompt)
        elif self.provider == "google":
            text = self._call_gemini(f"{system_prompt}\n\n{user_prompt}")
        else:
            text = self._call_openai_compat(system_prompt, user_prompt)

        # 如果绑定了工具，尝试解析工具调用
        tool_calls = []
        if self._bound_tools:
            tool_calls = _parse_tool_calls(text)
            if tool_calls:
                # 如果有工具调用，返回带 tool_calls 的 AIMessage
                if HAS_LANGCHAIN and LCMessage:
                    msg = LCMessage(content="", tool_calls=tool_calls)
                    return msg
                return FakeAIMessage(content="", tool_calls=tool_calls)

        # 返回标准 AIMessage（LangChain 管道兼容）
        if HAS_LANGCHAIN and LCMessage:
            return LCMessage(content=text)
        return FakeAIMessage(content=text, tool_calls=[])

    def bind_tools(self, tools: List[Any], **kwargs) -> "NativeLLMAdapter":
        """
        兼容 LangChain 的 .bind_tools() 方法

        将工具定义转换为 prompt 注入，返回绑定了工具的新适配器实例
        """
        # 创建新实例，避免污染原始对象
        bound = NativeLLMAdapter(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            max_retries=self.max_retries,
            timeout=self.timeout,
            **self.extra_kwargs,
        )
        bound.provider = self.provider
        bound._bound_tools = list(tools)
        bound._tools_prompt = _build_tools_prompt(tools)
        return bound

    def bind(self, **kwargs):
        """兼容 LangChain 的 .bind() 方法"""
        return self

    def with_config(self, **kwargs):
        """兼容 LangChain 的 .with_config() 方法"""
        return self

    # ================================================================
    #  消息解析
    # ================================================================

    def _parse_messages(self, messages: Any) -> tuple:
        """
        将各种格式的消息统一转换为 (system_prompt, user_prompt)

        支持：
          - LangChain Message 列表（HumanMessage, SystemMessage 等）
          - 元组列表 [("system", "..."), ("human", "...")]
          - 字符串
          - 字典列表 [{"role":"system","content":"..."}]
        """
        if isinstance(messages, str):
            return "", messages

        system_parts = []
        user_parts = []

        if not isinstance(messages, (list, tuple)):
            return "", str(messages)

        for msg in messages:
            # 元组格式: ("system", "content") 或 ("human", "content")
            if isinstance(msg, tuple) and len(msg) == 2:
                role, content = msg
                if role in ("system",):
                    system_parts.append(content)
                elif role in ("human", "user"):
                    user_parts.append(content)
                elif role in ("ai", "assistant"):
                    user_parts.append(f"[Assistant]: {content}")
                else:
                    user_parts.append(content)
            # LangChain Message 对象
            elif hasattr(msg, 'type') and hasattr(msg, 'content'):
                if msg.type == "system":
                    system_parts.append(msg.content)
                elif msg.type == "human":
                    user_parts.append(msg.content)
                elif msg.type == "ai":
                    # 如果 AI 消息有工具调用，格式化展示
                    if hasattr(msg, 'tool_calls') and msg.tool_calls:
                        tc_str = json.dumps(msg.tool_calls, ensure_ascii=False, default=str)
                        user_parts.append(f"[Assistant tool calls]: {tc_str}")
                    elif msg.content:
                        user_parts.append(f"[Assistant]: {msg.content}")
                elif msg.type == "tool":
                    # 工具返回结果
                    tool_name = getattr(msg, 'name', 'unknown_tool')
                    user_parts.append(f"[Tool Result - {tool_name}]: {msg.content}")
                else:
                    user_parts.append(str(msg.content) if hasattr(msg, 'content') else str(msg))
            # 字典格式
            elif isinstance(msg, dict):
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "system":
                    system_parts.append(content)
                elif role == "tool":
                    tool_name = msg.get("name", "unknown_tool")
                    user_parts.append(f"[Tool Result - {tool_name}]: {content}")
                else:
                    user_parts.append(content)
            # 其他
            else:
                user_parts.append(str(msg))

        return "\n".join(system_parts), "\n".join(user_parts)

    # ================================================================
    #  Anthropic 原生 SDK 调用（含三层防风控）
    # ================================================================

    def _call_anthropic(self, system_prompt: str, user_prompt: str) -> str:
        """
        调用 Anthropic Claude 模型 — 原生 SDK 直连

        三层防风控：
          第1层：致命熔断（401/403/余额不足 → 立即停手）
          第2层：限流退避（429/503 → 指数退避 + 随机抖动）
          第3层：超时重试（timeout/deadline → 短暂等待后重试）
        """
        from anthropic import Anthropic

        client = Anthropic(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=FAKE_HEADERS,
        )

        # 构建消息（Anthropic 不支持 system 在 messages 里，用 system 参数）
        kwargs = {
            "model": self.model,
            "max_tokens": 16000,
            "timeout": self.timeout,
        }

        # 构建 messages
        user_content = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
        kwargs["messages"] = [{"role": "user", "content": user_content}]

        # 如果是 thinking 模型，设置 temperature=1
        if "thinking" in self.model.lower():
            kwargs["temperature"] = 1

        for attempt in range(self.max_retries):
            try:
                response = client.messages.create(**kwargs)
                text_blocks = [b.text for b in response.content if b.type == "text"]
                return text_blocks[0] if text_blocks else ""

            except Exception as e:
                err_str = str(e).lower()

                # 第1层：致命熔断
                if "401" in err_str or "403" in err_str or "balance" in err_str:
                    logger.error(f"🚨 [Anthropic] 致命风控拦截，放弃重试: {e}")
                    return f"[API 错误] {e}"

                # 第2层：限流退避
                if "429" in err_str or "503" in err_str or "resource_exhausted" in err_str:
                    wait = min(2 ** attempt * 10, 60) + random.uniform(1.0, 5.0)
                    logger.warning(f"⏳ [Anthropic] 触发限流，退避 {wait:.1f}s（重试 {attempt+1}/{self.max_retries}）")
                    time.sleep(wait)
                    continue

                # 第3层：超时重试
                if "timeout" in err_str or "deadline" in err_str:
                    wait = 5 + random.uniform(1.0, 3.0)
                    logger.warning(f"⏳ [Anthropic] 超时，等待 {wait:.1f}s 重试（{attempt+1}/{self.max_retries}）")
                    time.sleep(wait)
                    continue

                # 其他错误
                logger.error(f"❌ [Anthropic] 异常: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(3 + random.uniform(0.5, 2.0))

        logger.error(f"❌ [Anthropic] {self.max_retries} 次重试全部失败")
        return "[API 错误] 重试全部失败"

    # ================================================================
    #  Google GenAI 原生 SDK 调用（含三层防风控）
    # ================================================================

    def _call_gemini(self, prompt: str) -> str:
        """
        调用 Google Gemini 模型 — 原生 SDK 直连

        三层防风控同上
        """
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(
            api_key=self.api_key,
            http_options={
                "api_version": "v1beta",
                "base_url": self.base_url,
                "headers": FAKE_HEADERS,
            },
        )

        config = genai_types.GenerateContentConfig(
            temperature=0.5,
            max_output_tokens=16000,
        )

        for attempt in range(self.max_retries):
            try:
                response = client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config,
                )
                return response.text if response.text else ""

            except Exception as e:
                err_str = str(e).lower()

                # 第1层：致命熔断
                if "401" in err_str or "403" in err_str or "balance" in err_str:
                    logger.error(f"🚨 [Gemini] 致命风控拦截，放弃重试: {e}")
                    return f"[API 错误] {e}"

                # 第2层：限流退避
                if "429" in err_str or "503" in err_str or "resource_exhausted" in err_str:
                    wait = min(2 ** attempt * 10, 60) + random.uniform(1.0, 5.0)
                    logger.warning(f"⏳ [Gemini] 触发限流，退避 {wait:.1f}s（重试 {attempt+1}/{self.max_retries}）")
                    time.sleep(wait)
                    continue

                # 第3层：超时重试
                if "timeout" in err_str or "deadline" in err_str:
                    wait = 5 + random.uniform(1.0, 3.0)
                    logger.warning(f"⏳ [Gemini] 超时，等待 {wait:.1f}s 重试（{attempt+1}/{self.max_retries}）")
                    time.sleep(wait)
                    continue

                # 其他错误
                logger.error(f"❌ [Gemini] 异常: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(3 + random.uniform(0.5, 2.0))

        logger.error(f"❌ [Gemini] {self.max_retries} 次重试全部失败")
        return "[API 错误] 重试全部失败"

    # ================================================================
    #  OpenAI 兼容协议回退（以防万一）
    # ================================================================

    def _call_openai_compat(self, system_prompt: str, user_prompt: str) -> str:
        """回退方案：其他模型仍走 OpenAI 兼容协议"""
        from openai import OpenAI

        client = OpenAI(
            api_key=self.api_key,
            base_url=f"{self.base_url}/v1" if not self.base_url.endswith("/v1") else self.base_url,
        )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        for attempt in range(self.max_retries):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=16000,
                    timeout=self.timeout,
                )
                return response.choices[0].message.content or ""
            except Exception as e:
                err_str = str(e).lower()
                if "401" in err_str or "403" in err_str:
                    return f"[API 错误] {e}"
                if attempt < self.max_retries - 1:
                    time.sleep(3 + random.uniform(0.5, 2.0))

        return "[API 错误] 重试全部失败"

    # ================================================================
    #  兼容属性
    # ================================================================

    def __repr__(self):
        tools_info = f", tools={len(self._bound_tools)}" if self._bound_tools else ""
        return f"NativeLLMAdapter(model={self.model!r}, provider={self.provider!r}{tools_info})"
