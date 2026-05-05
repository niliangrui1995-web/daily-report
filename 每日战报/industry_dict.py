"""
产业链 23 个核心细分赛道寡头监控字典 + 核心标的行情池。

从 daily_report.py 拆分，包含：
- OLIGARCH_DICT: 23 个核心细分赛道寡头字典
- ALL_OLIGARCHS_FLAT: 展平后的全量公司列表
- VANGUARD_TICKERS: 核心标的池（用于 yfinance 行情注入）
- SUPER_GIANTS: 万亿级市值超级巨头
- STRATEGIC_GIANTS: 二级超大盘/关键链主观察池
- classify_vanguard_signal(): 信号分级函数

【⚠️核心维护规则 / Agent 指令】
1. 凡向 OLIGARCH_DICT 新增公司时，必须查询其是否为独立上市企业。若已上市，**必须同步加入 VANGUARD_TICKERS**，并严格带上 yfinance 要求的国家后缀（.T=日本 .TW=台湾 .TWO=台湾上柜 .KS=韩国 .HK=香港 .AS=阿姆斯特丹 .PA=巴黎 .L=伦敦 .DE=法兰克福 .MI=米兰，美股无后缀）。若未上市（子公司/被收购），仅保留在 OLIGARCH_DICT；若核心业务由上市母公司承载，OLIGARCH_DICT 必须写成“上市母公司 (核心资产)”口径。
2. 中国大陆注册或核心经营主体位于中国大陆的公司，不进入 OLIGARCH_DICT/VANGUARD_TICKERS；台湾公司保留。
3. 当公司被收购或退市时，必须从 VANGUARD_TICKERS 中移除其 Ticker（可在 OLIGARCH_DICT 中用注释标注退市原因留档），参考 Shinko 退市处理的先例。
4. `classify_vanguard_signal()` 的信号阈值（超级巨头 ±3%/±5%，中型 ±5%/±8%）和 SUPER_GIANTS 名单是核心商业判断，修改前必须与策略决策者确认，不可凭直觉随意调整；STRATEGIC_GIANTS 只用于观察池强调，不享受超级巨头低阈值。
"""

OLIGARCH_DICT = {
    'EDA与底层IP': ['Synopsys', 'Cadence', 'ARM', 'Siemens EDA', 'Arteris', 'Alphawave (被Qualcomm收购已退市)'],
    'AI加速芯片与定制ASIC': ['NVIDIA', 'AMD', 'Broadcom (博通)', 'Marvell (迈威尔)', 'Alchip (世芯)', 'Qualcomm'],
    '先进制程代工': ['TSMC (台积电)', 'Samsung Foundry', 'Intel Foundry Services', 'GlobalFoundries'],
    '前道晶圆设备与量测': ['ASML', 'Applied Materials (应材)', 'Lam Research (泛林)', 'Tokyo Electron (TEL/东京电子)', 'KLA', 'Onto Innovation'],
    '关键晶圆材料与特种工艺': ['Shin-Etsu (信越化学)', 'SUMCO', 'Soitec', 'IQE', 'PVA TePla', 'Entegris'],
    'HBM与先进存储': ['SK Hynix (SK海力士)', 'Samsung Memory', 'Micron (美光)'],
    '先进封装与混合键合': ['Besi', 'EV Group', 'ASMPT', 'Hanmi Semi (韩美半导体)', 'Disco (迪思科)', 'TSMC (台积电)', 'ASE (日月光)', 'Amkor (安靠)'],
    'IC载板与封装材料': ['Ibiden (揖斐电)', 'Unimicron (欣兴)', 'Samsung E-M (三星电机)', 'Nan Ya PCB (南亚电路板)', 'Ajinomoto (味之素)', 'NAMICS (纳美仕)', 'SKC (Absolics)', 'Corning (康宁)'],
    '数据中心网络与高速电互连': ['Broadcom (博通)', 'NVIDIA', 'Marvell (迈威尔)', 'Arista Networks', 'Astera Labs', 'Credo', 'Amphenol (安费诺)', 'TE Connectivity'],
    '光模块与光引擎': ['Coherent (高意)', 'Fabrinet', 'Ciena'],
    '光芯片与硅光': ['Coherent (高意)', 'Lumentum (朗明特)', 'Intel Silicon Photonics', 'Ayar Labs', 'Lightmatter', 'Sumitomo E (住友电工)'],
    '光模块电芯片': ['Broadcom (博通)', 'Marvell (迈威尔)', 'Semtech', 'MACOM', 'MaxLinear', 'Credo'],
    '光通信无源器件与精密零部件': ['Amphenol (安费诺)', 'TE Connectivity', 'Corning Optical (康宁光纤)', 'Fujikura (藤仓)', 'US Conec', 'SENKO', 'Molex'],
    '高频PCB与覆铜板材料': ['Rogers (罗杰斯)', 'Panasonic MEGTRON', 'EMC (台光电)', 'Iteq (联茂)', 'Kingboard Laminates (建滔积层板)', 'Doosan (斗山)', 'Nan Ya (南亚)', 'Nittobo (日东纺)', 'Asahi Kasei (旭化成)', 'Resonac (力森诺科)', 'MGC (三菱瓦斯化学)', 'Mitsui Mining (三井金属)', 'Furukawa (古河电工)', 'Wus (楠梓电)', 'Compeq (华通)', 'Tripod (健鼎)'],
    'AI PCB设备与关键耗材': ['KLA (Orbotech/LDI/AOI)', 'MKS Instruments (Atotech/ESI)', 'SCREEN Holdings (Ledia)', 'Nidec Advance Technology', 'Mycronic (atg Luther & Maelzer)', 'AMADA Via Mechanics', 'SCHMID Group', 'Union Tool', 'Ushio (ADTEC Engineering)'],
    '数据中心电力与配电': ['Vertiv (维谛)', 'Eaton (伊顿)', 'Schneider (施耐德)', 'nVent', 'Delta (台达电)', 'MPS', 'Murata (村田/MLCC)'],
    '液冷与散热材料': ['CoolIT', 'Auras (奇鋐)', 'AVC (双鸿)', 'Delta (台达电)', 'Nidec (日本电产)', 'AGC (旭硝子)'],
    '半导体测试设备与探针卡': ['Advantest', 'Teradyne', 'FormFactor', 'Accretech (Tokyo Seimitsu)', 'KYEC (京元电子)', 'MJC', 'Technoprobe', 'MPI'],
    '非GPU计算架构': ['Groq', 'Cerebras', 'Tenstorrent'],
    '云巨头/算力租赁/数据基础设施': ['Microsoft (微软)', 'Meta', 'Google', 'Amazon/AWS', 'Oracle', 'OpenAI', 'Anthropic', 'CoreWeave', 'Scale AI'],
    '边缘AI芯片与终端平台': ['Apple (苹果)', 'Qualcomm (高通)', 'MediaTek (联发科)', 'Intel'],
    '具身智能与机器人平台': ['Tesla (特斯拉)', 'Figure AI', 'NVIDIA Robotics'],
    '光纤光缆与宽带接入': ['Corning Optical (康宁光纤)', 'Prysmian (普睿司曼)', 'Amphenol (CommScope CCS)', 'Vistance Networks (原CommScope ANS/RUCKUS)', 'Ciena', 'Calix', 'ADTRAN']
}

ALL_OLIGARCHS_FLAT: list[str] = []
for _entities in OLIGARCH_DICT.values():
    ALL_OLIGARCHS_FLAT.extend(_entities)

VANGUARD_TICKERS = {
    'Synopsys': 'SNPS',
    'Cadence': 'CDNS',
    'ARM': 'ARM',
    'Siemens EDA': 'SIE.DE',
    'Arteris': 'AIP',
    'Qualcomm': 'QCOM',
    'NVIDIA': 'NVDA',
    'AMD': 'AMD',
    'TSMC': 'TSM',
    'GlobalFoundries': 'GFS',
    'Broadcom': 'AVGO',
    'Marvell': 'MRVL',
    'Alchip': '3661.TW',
    'ASML': 'ASML',
    'Applied Materials': 'AMAT',
    'Lam Research': 'LRCX',
    'Tokyo Electron': '8035.T',
    'Shin-Etsu': '4063.T',
    'SUMCO': '3436.T',
    'Soitec': 'SOI.PA',
    'IQE': 'IQE.L',
    'PVA TePla': 'TPE.DE',
    'Entegris': 'ENTG',
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
    'SKC': '011790.KS',
    'Arista Networks': 'ANET',
    'Astera Labs': 'ALAB',
    'Credo': 'CRDO',
    'Amphenol': 'APH',
    'TE Connectivity': 'TEL',
    'Coherent': 'COHR',
    'Fabrinet': 'FN',
    'Lumentum': 'LITE',
    'Sumitomo E': '5802.T',
    'Semtech': 'SMTC',
    'MACOM': 'MTSI',
    'MaxLinear': 'MXL',
    'Fujikura': '5803.T',
    'Rogers': 'ROG',
    'Panasonic': '6752.T',
    'EMC': '2383.TW',
    'Iteq': '6213.TW',
    'Kingboard Laminates': '1888.HK',
    'Doosan': '000150.KS',
    'Nan Ya': '1303.TW',
    'Nittobo': '3110.T',
    'Asahi Kasei': '3407.T',
    'Resonac': '4004.T',
    'MGC': '4182.T',
    'Mitsui Mining': '5706.T',
    'Furukawa': '5801.T',
    'Wus': '2316.TW',
    'Compeq': '2313.TW',
    'Tripod': '3044.TW',
    'MKS Instruments': 'MKSI',
    'SCREEN Holdings': '7735.T',
    'Mycronic': 'MYCR.ST',
    'AMADA': '6113.T',
    'SCHMID Group': 'SHMD',
    'Union Tool': '6278.T',
    'Ushio': '6925.T',
    'nVent': 'NVT',
    'Vertiv': 'VRT',
    'Eaton': 'ETN',
    'Schneider': 'SU.PA',
    'Delta': '2308.TW',
    'Auras': '3324.TWO',
    'AVC': '3017.TW',
    'MPS': 'MPWR',
    'Murata': '6981.T',
    'AGC': '5201.T',
    'Nidec': '6594.T',
    'Advantest': '6857.T',
    'Teradyne': 'TER',
    'FormFactor': 'FORM',
    'Accretech': '7729.T',
    'KYEC': '2449.TW',
    'MJC': '6871.T',
    'Disco': '6146.T',
    'MPI': '6223.TWO',
    'Technoprobe': 'TPRO.MI',
    'Intel': 'INTC',
    'Microsoft': 'MSFT',
    'Meta': 'META',
    'Google': 'GOOGL',
    'Amazon': 'AMZN',
    'Oracle': 'ORCL',
    'CoreWeave': 'CRWV',
    'Apple': 'AAPL',
    'Tesla': 'TSLA',
    'MediaTek': '2454.TW',
    'Ciena': 'CIEN',
    'Calix': 'CALX',
    'Vistance Networks': 'VISN',
    'ADTRAN': 'ADTN',
    'Prysmian': 'PRY.MI'
}

SUPER_GIANTS = set(['Meta', 'Apple', 'Broadcom', 'NVIDIA', 'Google', 'Tesla', 'TSMC', 'Microsoft', 'ASML', 'Amazon', 'Samsung'])

STRATEGIC_GIANTS = set([
    'SK Hynix',
    'Micron',
    'AMD',
    'Oracle',
    'Intel',
    'Applied Materials',
    'Lam Research',
    'KLA',
    'ARM',
    'Arista Networks',
    'Qualcomm',
    'TE Connectivity',
    'Marvell',
])

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
