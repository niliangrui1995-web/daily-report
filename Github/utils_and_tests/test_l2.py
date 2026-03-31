import os
import json
from openai import OpenAI

# 代理地址
ANTIGRAVITY_BASE_URL = os.environ.get("ANTIGRAVITY_BASE_URL", "http://127.0.0.1:8045/v1")
ANTIGRAVITY_API_KEY = os.environ.get("ANTIGRAVITY_API_KEY", "REMOVED_CREDENTIAL")
L2_MODEL = "claude-opus-4-6"

client = OpenAI(
    api_key=ANTIGRAVITY_API_KEY,
    base_url=ANTIGRAVITY_BASE_URL,
)

def test_l2():
    print("开始测试 L2 Claude Opus 4.6 挂载...")
    dummy_l1_data = {
        "data": [
            {"Entity": "日东纺", "Metric": "NE-Glass Yarn 出货", "Value": "爆满排单，交单延迟12周", "Confidence_Score": 0.95},
            {"Entity": "台光电", "Metric": "M7覆铜板", "Value": "本月涨价 15%", "Confidence_Score": 0.9}
        ]
    }
    
    l1_data_str = json.dumps(dummy_l1_data, ensure_ascii=False)
    prompt = f"""
你是一名深谙 A 股投机规律的幕后逻辑判官。请根据海外 L1 捕捉回来的绝对纯净物理参数组，推演在国内市场的投射。
海外铁证数据：
{l1_data_str}

【你的判决使命】：
1. 根据上述海外“断供/涨价/技术突破”，公网检索 A 股实打实的能蹭上的企业（去伪存真查最新扩产动向）。
2. 【绝对禁止计算确切估值或利润增长百分比】，我不要你的算术。
3. 请依据“模糊的正确”哲学，把排查出的 A 公司按弹性划分为：
   - [高弹性战区]：业务占比大，或刚刚宣布转固/打入果链英链。
   - [中弹性助攻]：二供选手，或存在模糊博弈空间。
   - [纯概念雷区]：互动易嘴炮，无 CapEx 实际支撑。
4. 生成一份充斥 A 股极寒逻辑的 Markdown 狙击战报给长官扣动扳机。
"""
    try:
        response = client.chat.completions.create(
            model=L2_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=4096
        )
        print("\n========= L2 推演成功 =========")
        print(response.choices[0].message.content)
        print("===============================")
    except Exception as e:
        print(f"L2 测试失败: {e}")

if __name__ == "__main__":
    test_l2()
