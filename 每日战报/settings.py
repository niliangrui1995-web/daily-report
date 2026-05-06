__doc__ = '统一配置中心 — 环境变量加载 + 全局常量 + 适配器单例。\n\n【⚠️核心维护规则 / Agent 指令】\n1. 所有 API Key（Anthropic/Gemini/Grok/Tavily/SMTP）必须且只能从 `.env` 环境变量读取，严禁硬编码到代码里。\n2. `_auto_detect_local_proxy()` 的端口探活逻辑是防翻墙死锁的保命机制——它在 0.2s 内依次拨测常见代理端口。修改端口列表或超时值时必须确保 NO_PROXY 包含 `127.0.0.1,localhost`，防止本地反代 8045 被自己的代理误拦截形成死循环。\n3. `_LazyAdapter` 惰性加载模式是为了避免 `settings.py` 在模块顶层就实例化 NativeLLMAdapter 导致循环 import 死锁。严禁改成 `pro_adapter = NativeLLMAdapter(...)` 的立即初始化形式。\n'

import os
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / '.env')
except ImportError:
    pass

def _merge_no_proxy(existing: str | None, required_hosts: list[str]) -> str:
    merged: list[str] = []
    seen: set[str] = set()

    def _append(value: str) -> None:
        normalized = value.strip()
        if not normalized:
            return
        lowered = normalized.lower()
        if lowered in seen:
            return
        seen.add(lowered)
        merged.append(normalized)

    for raw in (existing or "").split(","):
        _append(raw)
    for host in required_hosts:
        _append(host)
    return ",".join(merged)


NO_PROXY_REQUIRED_HOSTS = ['127.0.0.1', 'localhost', '::1']
MERGED_NO_PROXY = _merge_no_proxy(os.environ.get('NO_PROXY'), NO_PROXY_REQUIRED_HOSTS)
os.environ['NO_PROXY'] = MERGED_NO_PROXY
os.environ['no_proxy'] = MERGED_NO_PROXY
SYSTEM_PROXY = None
for k in ('HTTP_PROXY', 'HTTPS_PROXY'):
    if k in os.environ:
        if not SYSTEM_PROXY:
            SYSTEM_PROXY = os.environ[k]


def _parse_insecure_tls_allowlist(raw_value: str | None) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for item in (raw_value or '').split(','):
        normalized = item.strip().lower()
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        values.append(normalized)
    return tuple(values)


INSECURE_TLS_ALLOWLIST = _parse_insecure_tls_allowlist(os.environ.get('INSECURE_TLS_ALLOWLIST'))
TLS_POLICY = {
    'default_verify': True,
    'insecure_allowlist': list(INSECURE_TLS_ALLOWLIST),
}


def get_tls_policy_summary() -> str:
    allowlist = ','.join(INSECURE_TLS_ALLOWLIST) if INSECURE_TLS_ALLOWLIST else 'none'
    return f"strict_default; allowlist={allowlist}"


def is_insecure_tls_allowed(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ''
    except Exception:
        host = ''
    host = host.lower().strip()
    if not host:
        return False
    for candidate in INSECURE_TLS_ALLOWLIST:
        if host == candidate or host.endswith(f'.{candidate}'):
            return True
    return False

ANTIGRAVITY_BASE_URL = os.environ.get('ANTIGRAVITY_BASE_URL', 'http://127.0.0.1:8045')
ANTIGRAVITY_API_KEY = os.environ.get('ANTIGRAVITY_API_KEY', '')
if not ANTIGRAVITY_API_KEY:
    logging.warning('⚠️ ANTIGRAVITY_API_KEY 未配置！请检查 .env 文件。Pro/Opus 模型调用将鉴权失败。')
PRO_OPUS_BASE_URL = os.environ.get('PRO_OPUS_BASE_URL', ANTIGRAVITY_BASE_URL)
PRO_OPUS_API_KEY = os.environ.get('PRO_OPUS_API_KEY', ANTIGRAVITY_API_KEY)

GEMINI_SEARCH_DIRECT_URL = 'https://generativelanguage.googleapis.com'
GEMINI_SEARCH_CF_URL = os.environ.get('GEMINI_SEARCH_BASE_URL', 'https://api.niliangrui.cloud')
_search_keys_env = os.environ.get('GEMINI_SEARCH_API_KEYS', '')
_search_single_key = os.environ.get('GEMINI_SEARCH_API_KEY', '')
if _search_keys_env:
    SEARCH_API_KEY_POOL = [k.strip() for k in _search_keys_env.split(',') if k.strip()]
elif _search_single_key:
    SEARCH_API_KEY_POOL = [_search_single_key]
else:
    SEARCH_API_KEY_POOL = []

if not SEARCH_API_KEY_POOL:
    logging.warning('⚠️ GEMINI_SEARCH_API_KEYS 未配置！请检查 .env 文件。联网搜索功能将不可用。')

GEMINI_SEARCH_MODEL = 'gemini-2.5-flash'
GEMINI_PRO_MODEL = 'gemini-3.1-pro-high'
OPUS_MODEL = 'claude-opus-4-6-thinking'
AGENT_ASSIST_ENABLED = os.environ.get('AGENT_ASSIST_ENABLED', '').strip().lower() in {'1', 'true', 'yes', 'on'}
AGENT_ASSIST_MODEL = os.environ.get('AGENT_ASSIST_MODEL', '').strip()
AGENT_ASSIST_BASE_URL = os.environ.get('AGENT_ASSIST_BASE_URL', '').strip()
AGENT_ASSIST_API_KEY = os.environ.get('AGENT_ASSIST_API_KEY', '').strip()

TAVILY_API_KEY = os.environ.get('TAVILY_API_KEY', '')
GROK_API_KEY = os.environ.get('GROK_API_KEY', '')

EMAIL_ADDRESS = os.environ.get('EMAIL_ADDRESS', '')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', '')
RECEIVER_EMAIL = os.environ.get('RECEIVER_EMAIL', '')

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '每日热点输出')
os.makedirs(OUTPUT_DIR, exist_ok=True)

def get_today_output_dir() -> str:
    today_str = datetime.now().strftime('%Y%m%d')
    day_dir = os.path.join(OUTPUT_DIR, today_str)
    os.makedirs(day_dir, exist_ok=True)
    return day_dir

_prompt_cache: dict[str, str] = {}

def load_prompt(name: str, *, required: bool = False) -> str:
    if name not in _prompt_cache:
        prompt_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')
        prompt_path = os.path.join(prompt_dir, name)
        try:
            with open(prompt_path, 'r', encoding='utf-8') as f:
                _prompt_cache[name] = f.read()
        except Exception as e:
            logging.error(f'⚠️ [配置系统] 加载 Prompt 模板失败: {name}, Error: {e}')
            _prompt_cache[name] = ''
    prompt_text = _prompt_cache[name]
    if required and not prompt_text.strip():
        raise FileNotFoundError(f'关键 Prompt 不存在或为空: {name}')
    return prompt_text


def require_prompt(name: str) -> str:
    """关键阶段使用：Prompt 缺失时显式失败，而不是悄悄返回空串。"""
    return load_prompt(name, required=True)

_pro_adapter = None
_opus_adapter = None
_agent_adapter = None

def _get_pro_adapter():
    global _pro_adapter
    if _pro_adapter is None:
        from native_llm_adapter import NativeLLMAdapter
        _pro_adapter = NativeLLMAdapter(model=GEMINI_PRO_MODEL, base_url=PRO_OPUS_BASE_URL, api_key=PRO_OPUS_API_KEY, max_retries=3)
    return _pro_adapter

def _get_opus_adapter():
    global _opus_adapter
    if _opus_adapter is None:
        from native_llm_adapter import NativeLLMAdapter
        _opus_adapter = NativeLLMAdapter(model=OPUS_MODEL, base_url=PRO_OPUS_BASE_URL, api_key=PRO_OPUS_API_KEY, timeout=600.0, max_retries=5)
    return _opus_adapter


def get_agent_adapter():
    global _agent_adapter
    if not AGENT_ASSIST_ENABLED:
        return None
    if not AGENT_ASSIST_MODEL:
        logging.warning('⚠️ AGENT_ASSIST_ENABLED 已开启，但 AGENT_ASSIST_MODEL 未配置；跳过 Agent 助手。')
        return None
    if _agent_adapter is None:
        from native_llm_adapter import NativeLLMAdapter
        _agent_adapter = NativeLLMAdapter(
            model=AGENT_ASSIST_MODEL,
            base_url=AGENT_ASSIST_BASE_URL or ANTIGRAVITY_BASE_URL,
            api_key=AGENT_ASSIST_API_KEY or ANTIGRAVITY_API_KEY,
            timeout=180.0,
            max_retries=2,
        )
    return _agent_adapter

class _LazyAdapter:
    '''属性代理，兼容 `from settings import pro_adapter` 的直接使用方式。

    Why: 代理 __getattr__ + __repr__ + __call__，确保直接调用也能正确转发。
    '''
    def __init__(self, getter):
        self._getter = getter
    def __getattr__(self, name):
        return getattr(self._getter(), name)
    def __repr__(self):
        return repr(self._getter())
    def __call__(self, *args, **kwargs):
        return self._getter()(*args, **kwargs)

pro_adapter = _LazyAdapter(_get_pro_adapter)
opus_adapter = _LazyAdapter(_get_opus_adapter)
