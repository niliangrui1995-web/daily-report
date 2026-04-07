"""
产业链 15 赛道全覆盖寡头监控字典 + 美股标的池。

从 daily_report.py 拆分，包含：
- OLIGARCH_DICT: 15 赛道寡头字典
- ALL_OLIGARCHS_FLAT: 展平后的全量公司列表
- VANGUARD_TICKERS: 核心标的池（用于 yfinance 行情注入）
- SUPER_GIANTS: 万亿级市值超级巨头
- classify_vanguard_signal(): 信号分级函数

【⚠️核心维护规则 / Agent 指令】
1. 凡向 OLIGARCH_DICT 新增公司时，必须查询其是否为独立上市企业。若已上市，**必须同步加入 VANGUARD_TICKERS**，并严格带上 yfinance 要求的国家后缀（.T=日本 .TW=台湾 .KS=韩国 .HK=香港 .AS=阿姆斯特丹 .PA=巴黎 .L=伦敦 .DE=法兰克福 .MI=米兰，美股无后缀）。若未上市（子公司/被收购），仅保留在 OLIGARCH_DICT。
2. 当公司被收购或退市时，必须从 VANGUARD_TICKERS 中移除其 Ticker（可在 OLIGARCH_DICT 中用注释标注退市原因留档），参考 Shinko 退市处理的先例。
3. `classify_vanguard_signal()` 的信号阈值（超级巨头 ±3%/±5%，中型 ±5%/±8%）和 SUPER_GIANTS 名单是核心商业判断，修改前必须与策略决策者确认，不可凭直觉随意调整。
"""

OLIGARCH_DICT = {
    '芯片设计与底层IP': ['Synopsys', 'Cadence', 'ARM', 'Arteris', 'Qualcomm', 'Alphawave'],
    '定制化ASIC与代工': ['TSMC (台积电)', 'Intel Foundry Services', 'Broadcom (博通)', 'Marvell (迈威尔)', 'Alchip (世芯)'],
    '晶圆制造与材料设备': ['ASML', 'Applied Materials (应材)', 'Lam Research (泛林)', 'TEL (东京电子)', 'KLA', 'Onto Innovation', 'Soitec', 'IQE', 'PVA TePla'],
    'HBM与核心存储矩阵': ['SK Hynix (SK海力士)', 'Samsung Memory', 'Micron (美光)', 'Hanmi Semi (韩美半导体)', 'NAMICS (纳美仕)'],
    '先进封装与混合键合': ['Besi', 'EV Group', 'ASMPT', 'TSMC (台积电)', 'ASE (日月光)', 'Amkor (安靠)'],
    '核心基板与封装材料': ['Ibiden (揖斐电)', 'Unimicron (欣兴)', 'Samsung E-M (三星电机)', 'Nan Ya PCB (南亚电路板)', 'Ajinomoto (味之素)', 'Absolics', 'Corning (康宁)'],
    '网络交换与高速电互连': ['Broadcom (博通)', 'NVIDIA', 'Marvell (迈威尔)', 'Astera Labs', 'Credo', 'Amphenol (安费诺)', 'TE Connectivity'],
    '光模块/硅光/CPO': ['Intel', 'Innolight (中际旭创)', 'Coherent (高意)', 'Eoptolink (新易盛)', 'Lumentum (朗明特)', 'Sumitomo E (住友电工)', 'Ayar Labs', 'Lightmatter', 'Fabrinet', 'Granopt'],
    '高频PCB与覆铜板材料': ['Rogers (罗杰斯)', 'Panasonic MEGTRON', 'EMC (台光电)', 'Iteq (联茂)', 'Doosan (斗山)', 'Nan Ya (南亚)', 'Nittobo (日东纺)', 'Asahi Kasei (旭化成)', 'Resonac (力森诺科)', 'MGC (三菱瓦斯化学)', 'Mitsui Mining (三井金属)', 'Furukawa (古河电工)', 'Wus (华通)', 'Compeq (健鼎)'],
    'AI服务器基建与温控': ['nVent', 'Vertiv (维谛)', 'Eaton (伊顿)', 'Schneider (施耐德)', 'Delta (台达电)', 'MPS', 'CoolIT', 'Auras (奇鋐)', 'AVC (双鸿)', 'AGC (旭硝子)', 'Nidec (日本电产)'],
    '测试/老化与探针卡': ['Advantest', 'Teradyne', 'KYEC (京元电子)', 'FormFactor', 'Disco (迪思科)', 'MJC', 'Technoprobe', 'MPI'],
    '算力颠覆与主权生态': ['Groq', 'Cerebras', 'Tenstorrent', 'SMIC (中芯国际)', 'Huawei (华为)', 'NAURA (北方华创)', 'AMEC (中微)'],
    '云巨头/算力租赁与数据': ['Microsoft (微软)', 'Meta', 'Google', 'AWS', 'OpenAI', 'Anthropic', 'CoreWeave', 'Scale AI'],
    '边缘AI与具身智能': ['Apple (苹果)', 'Qualcomm (高通)', 'MediaTek (联发科)', 'Figure AI', 'Tesla (特斯拉)'],
    '光纤光缆与通信基建': ['Corning Optical (康宁光纤)', 'Prysmian (普瞐司曼)', 'CommScope', 'Ciena', 'Calix', 'ADTRAN']
}

ALL_OLIGARCHS_FLAT: list[str] = []
for _entities in OLIGARCH_DICT.values():
    ALL_OLIGARCHS_FLAT.extend(_entities)

VANGUARD_TICKERS = {
    'Synopsys': 'SNPS',
    'Cadence': 'CDNS',
    'ARM': 'ARM',
    'Qualcomm': 'QCOM',
    'Alphawave': 'AWE.L',
    'TSMC': 'TSM',
    'Broadcom': 'AVGO',
    'Marvell': 'MRVL',
    'Alchip': '3661.TW',
    'ASML': 'ASML',
    'Applied Materials': 'AMAT',
    'Lam Research': 'LRCX',
    'TEL': '8035.T',
    'Soitec': 'SOI.PA',
    'IQE': 'IQE.L',
    'PVA TePla': 'TPE.DE',
    'KLA': 'KLAC',
    'Onto Innovation': 'ONTO',
    'SK Hynix': '000660.KS',
    'Samsung': '005930.KS',
    'Micron': 'MU',
    'Hanmi Semi': '042700.KS',
    'Besi': 'BESI.AS',
    'Amkor': 'AMKR',
    'ASMPT': '0522.HK',
    'ASE': '3711.TW',
    'Corning': 'GLW',
    'Ibiden': '4062.T',
    'Unimicron': '3037.TW',
    'Ajinomoto': '2802.T',
    'Samsung E-M': '009150.KS',
    'Nan Ya PCB': '8046.TW',
    'NVIDIA': 'NVDA',
    'Astera Labs': 'ALAB',
    'Credo': 'CRDO',
    'Amphenol': 'APH',
    'TE Connect': 'TEL',
    'Coherent': 'COHR',
    'Fabrinet': 'FN',
    'Lumentum': 'LITE',
    'Sumitomo E': '5802.T',
    'Rogers': 'ROG',
    'Panasonic': '6752.T',
    'EMC': '2383.TW',
    'Iteq': '6213.TW',
    'Doosan': '000150.KS',
    'Nan Ya': '1303.TW',
    'Nittobo': '3110.T',
    'Asahi Kasei': '3407.T',
    'Resonac': '4004.T',
    'MGC': '4182.T',
    'Mitsui Mining': '5706.T',
    'Furukawa': '5801.T',
    'Wus': '2313.TW',
    'Compeq': '3044.TW',
    'nVent': 'NVT',
    'Vertiv': 'VRT',
    'Eaton': 'ETN',
    'Schneider': 'SU.PA',
    'Delta': '2308.TW',
    'Auras': '3324.TWO',
    'AVC': '3017.TW',
    'MPS': 'MPWR',
    'AGC': '5201.T',
    'Nidec': '6594.T',
    'Advantest': '6857.T',
    'Teradyne': 'TER',
    'FormFactor': 'FORM',
    'KYEC': '2449.TW',
    'Disco': '6146.T',
    'MPI': '6223.TWO',
    'Technoprobe': 'TPRO.MI',
    'AMD': 'AMD',
    'Intel': 'INTC',
    'Microsoft': 'MSFT',
    'Meta': 'META',
    'Google': 'GOOGL',
    'Amazon': 'AMZN',
    'CoreWeave': 'CRWV',
    'Apple': 'AAPL',
    'Tesla': 'TSLA',
    'MediaTek': '2454.TW',
    'Ciena': 'CIEN',
    'Calix': 'CALX',
    'CommScope': 'COMM',
    'ADTRAN': 'ADTN',
    'Prysmian': 'PRY.MI'
}

SUPER_GIANTS = set(['Meta', 'Apple', 'Broadcom', 'NVIDIA', 'Google', 'Tesla', 'TSMC', 'Microsoft', 'ASML', 'Amazon'])

def classify_vanguard_signal(name: str, pct_change: float) -> str:
    if name in SUPER_GIANTS:
        if pct_change >= 3:
            return '🚀 大涨催化'
        if pct_change <= -5:
            return '💥 暴跌警惕'
        if pct_change <= -2:
            return '📉 下跌注意'
        return '📈 正常'
    
    if pct_change >= 5:
        return '🚀 大涨催化'
    if pct_change <= -8:
        return '💥 暴跌警惕'
    if pct_change <= -3:
        return '📉 下跌注意'
        
    return '📈 正常'
