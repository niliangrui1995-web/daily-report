import urllib.request
import json

graph = """
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#111111', 'primaryTextColor': '#00ff00', 'primaryBorderColor': '#00ff00', 'lineColor': '#00ff00', 'clusterBkg': '#0a0a0a', 'clusterBorder': '#00aa00', 'edgeLabelBackground':'#111111', 'textColor': '#00ff00'}}}%%
flowchart TD
    subgraph P1["Python 主控区"]
        t([定时触发]) --> yt{查询财报日}
        yt -- "T+1天" --> B(模式B)
        yt -- ">1天" --> g1{Gemini探测}
        g1 -- 发现大会 --> C(模式C)
        g1 -- 常规时期 --> A(模式A)
    end

    subgraph P2["Prompt 组装区"]
        B --> pb[财报问答实录]
        C --> pc[底层物理参数]
        A --> pa[极客白名单70%圈定]
    end

    subgraph P3["防噪提炼(Gemini)"]
        pb --> gn([限定权限爬取])
        pc --> gn
        pa --> gn
        gn --> cx{数据防噪清洗}
        cx -- 纯口水情绪废料 --> drop[判定废料静默丢弃]
        cx -- 大V抢跑或硬数据互证 --> extract[强制特权放行 无视一切滞后官方参数:<br>萃取变压器缺口与良率突变信号]
    end

    subgraph P4["A股核心映射投递"]
        extract --> map[雷达定点捕捉 A股 对应赛道代码]
        map --> send(((触发终端报警消息投递)))
    end
"""

url = "https://kroki.io/mermaid/svg"
headers = {
    'Content-Type': 'text/plain',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
}
data = graph.encode('utf-8')

print(f"Fetching via POST: {url}...")
try:
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=20) as response:
        with open("VCP_Hunter_全景工作流_Cyber.svg", "wb") as f:
            f.write(response.read())
    print("Successfully saved to VCP_Hunter_全景工作流_Cyber.svg via POST.")
except Exception as e:
    print(f"Error: {e}")
