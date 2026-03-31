# -*- coding: utf-8 -*-
"""快速测试 gemini-3.1-pro-high 和 claude-opus-4-6-thinking 连接"""
import sys
import time
sys.stdout.reconfigure(line_buffering=True)

from native_llm_adapter import NativeLLMAdapter

BASE = "http://127.0.0.1:8045"
KEY = "REMOVED_CREDENTIAL"

# TEST 1: Gemini 3.1 Pro High
print("=" * 50)
print("TEST 1: gemini-3.1-pro-high")
print("=" * 50)
t0 = time.time()
try:
    adapter = NativeLLMAdapter(
        model="gemini-3.1-pro-high",
        base_url=BASE,
        api_key=KEY,
        timeout=30.0,
    )
    result = adapter.invoke("Say hello in one sentence")
    elapsed = time.time() - t0
    print(f"✅ OK! ({elapsed:.1f}s)")
    print(f"   Response: {result.content[:200]}")
except Exception as e:
    elapsed = time.time() - t0
    print(f"❌ FAILED ({elapsed:.1f}s)")
    print(f"   Error: {e}")

time.sleep(3)

# TEST 2: Claude Opus 4.6 Thinking
print()
print("=" * 50)
print("TEST 2: claude-opus-4-6-thinking")
print("=" * 50)
t0 = time.time()
try:
    adapter = NativeLLMAdapter(
        model="claude-opus-4-6-thinking",
        base_url=BASE,
        api_key=KEY,
        timeout=30.0,
    )
    result = adapter.invoke("Say hello in one sentence")
    elapsed = time.time() - t0
    print(f"✅ OK! ({elapsed:.1f}s)")
    print(f"   Response: {result.content[:200]}")
except Exception as e:
    elapsed = time.time() - t0
    print(f"❌ FAILED ({elapsed:.1f}s)")
    print(f"   Error: {e}")

print()
print("ALL TESTS DONE!")
