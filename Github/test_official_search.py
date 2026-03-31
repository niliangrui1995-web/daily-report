import os
import sys

# 提示：在此处填入您申请到的 Google 官方直连 API Key
# 或者在运行前设置环境变量: set GEMINI_SEARCH_API_KEY="AIzaSy..."
test_api_key = os.environ.get("GEMINI_SEARCH_API_KEY", "填入您的原生API_KEY")

if test_api_key == "填入您的原生API_KEY" or not test_api_key:
    print("❌ 请首先在代码中填写您的官方 API Key，或者设置环境变量 GEMINI_SEARCH_API_KEY。")
    sys.exit(1)

print(f"正在测试官方直连 API Key: {test_api_key[:10]}... (隐藏后缀)")
print("请确保您当前系统或终端已挂载科学上网代理（Clash / V2ray 等全局/TUN模式或配置了 http_proxy）\n")

# 应用用户提供的 VilaNet 官方代理端口 (纯 SOCKS5)
os.environ["ALL_PROXY"] = "socks5://127.0.0.1:10081"

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("❌ 缺少依赖，请先安装: pip install google-genai")
    sys.exit(1)

# 注意：不传 base_url，完全走默认的官方 endpoint
# 如果在国内，需要配合终端科学上网环境变量 (如 set http_proxy=http://127.0.0.1:xxxx)
FAKE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

try:
    print("🔹 [1/2] 正在初始化直连 Client...")
    gemini_search_client = genai.Client(
        api_key=test_api_key,
        http_options={'api_version': 'v1beta', 'headers': FAKE_HEADERS, 'timeout': 60.0}
    )

    print("🔹 [2/2] 正在发送 Google Search Grounding 联网搜索请求...")
    # 测试联网获取即时信息
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=1.0,
        max_output_tokens=1024,
    )
    
    response = gemini_search_client.models.generate_content(
        model="gemini-2.5-flash",  # 这里最好填 2.5-flash，或者 3-flash 如果官方已支持该模型名
        contents="查询今天A股市场的最新研报和资金流向，列举前两个强势板块和理由。(尽量简短)",
        config=config,
    )
    
    print("\n✅ 测试成功！来自 Google 官方的数据返回：\n")
    print(response.text)
    
    # 获取搜索出处
    if getattr(response, 'candidates', None) and hasattr(response.candidates[0], 'grounding_metadata'):
        gm = response.candidates[0].grounding_metadata
        if hasattr(gm, 'grounding_chunks') and gm.grounding_chunks:
            print(f"\n🌍 检索到了 {len(gm.grounding_chunks)} 个真实网页来源：")
            for i, chunk in enumerate(gm.grounding_chunks[:3]):
                if hasattr(chunk, 'web') and chunk.web:
                    print(f"  {i+1}. {getattr(chunk.web, 'title', 'N/A')} - {getattr(chunk.web, 'uri', '')}")
                    
except Exception as e:
    print(f"\n❌ 测试失败 ({type(e).__name__}): {e}")
    print("可能的原因：")
    print("1. 您的原生 API Key 不正确或还未生效。")
    print("2. 您的终端没有走系统代理，无法直接连接到 Google 官方服务器，导致 Timeout / ConnectionError。")
    print("   (可尝试在终端手动执行 set https_proxy=http://127.0.0.1:你的代理端口 等配置)")
