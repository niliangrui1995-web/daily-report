__doc__ = '数据源配置文件 (从原 config.py 恢复，供 p1_rss 使用)\n\n【⚠️核心维护规则 / Agent 指令】\n1. 这是一个纯静态配置字典仓库。严禁在此处加入任何动态查询、网络 IO 或复杂的 imports 循环依赖，必须保持其极度干净的数据结构（data-only）属性。\n2. URL_BLACKLIST 和 CONTENT_NOISE_KEYWORDS 等字段是抵抗反爬验证页和订阅弹窗的核心防线，增删时必须小心测试。\n3. 如果因为被墙或改版废弃某个 RSS 源/网页源，首选将其注释掉并写明 Why，严禁直接删除，方便后续接手人考古其挂断原因。\n'

FAKE_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
FAKE_HEADERS = {'User-Agent': FAKE_UA}
REQUEST_TIMEOUT = 20
SEC_USER_AGENT = 'VCP Hunter daily-report/3.6 research@localhost.invalid'
SUMMARY_LEN = 5000

RSSHUB_INSTANCES = ['https://rsshub.app', 'https://rsshub.rssforever.com', 'https://rsshub-instance.zeabur.app']
NITTER_INSTANCES = ['https://xcancel.com', 'https://nitter.space', 'https://nitter.poast.org', 'https://nitter.privacydev.net']

TWITTER_WATCHLIST = [
    ['kopite7kimi', 'kopite7kimi'],
    ['Dylan Patel', 'dylan522p'],
    ['SemiAnalysis', 'SemiAnalysis'],
    ['Jukanlosreve', 'Jukanlosreve'],
    ['harukaze5719', 'harukaze5719'],
    ['VideoCardz', 'VideoCardz']
]

RSS_FEEDS = [
    {'name': 'Bloomberg_Tech', 'url': 'https://feeds.bloomberg.com/technology/news.rss', 'source': 'bloomberg', 'group': 'major'},
    {'name': 'CNBC_Tech', 'url': 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910', 'source': 'cnbc', 'group': 'major'},
    {'name': 'TechCrunch', 'url': 'https://techcrunch.com/feed/', 'source': 'techcrunch', 'group': 'major'},
    {'name': 'TechPowerUp', 'url': 'https://www.techpowerup.com/rss/news', 'source': 'techpowerup', 'group': 'major'},
    {'name': 'TomsHardware', 'url': 'https://www.tomshardware.com/feeds/all', 'source': 'tomshardware', 'group': 'major'},
    {'name': 'SeekingAlpha_Tech', 'url': 'https://seekingalpha.com/sector/technology.xml', 'source': 'seekingalpha', 'group': 'deep'},
    {'name': 'ServeTheHome', 'url': 'https://www.servethehome.com/feed/', 'source': 'servethehome', 'group': 'deep'},
    {'name': 'TheRegister_AI', 'url': 'https://www.theregister.com/software/ai_ml/headlines.atom', 'source': 'theregister_ai', 'group': 'deep'},
    {'name': 'AWS_ML_Blog', 'url': 'https://aws.amazon.com/blogs/machine-learning/feed/', 'source': 'aws_ml_blog', 'group': 'deep'},
    {'name': 'SemiEngineering', 'url': 'https://semiengineering.com/feed/', 'source': 'semiengineering', 'group': 'deep'},
    {'name': 'EETimes', 'url': 'https://www.eetimes.com/feed/', 'source': 'eetimes', 'group': 'deep'},
    {'name': 'TechNews_TW', 'url': 'https://technews.tw/feed/', 'source': 'technews_tw', 'group': 'major'},
    {'name': 'DataCenterKnowledge', 'url': 'https://www.datacenterknowledge.com/rss.xml', 'source': 'datacenterknowledge', 'group': 'deep'},
    {'name': 'TheElec_KR', 'url': 'https://www.thelec.kr/rss/allArticle.xml', 'source': 'thelec_kr', 'group': 'deep'},
    {'name': 'FierceElectronics', 'url': 'https://www.fierceelectronics.com/rss/xml', 'source': 'fierceelectronics', 'group': 'deep'}
]

REDDIT_SUBS = ['hardware', 'nvidia', 'amd']

SOURCE_CONTENT_SELECTORS = {
    'Bloomberg_Tech': ['.article-body p', '.body-content p', 'article p', "div[class*='article'] p"],
    'CNBC_Tech': ['.ArticleBody-articleBody p', '.FeaturedContent-articleBody p', "div[class*='ArticleBody'] p", 'article p'],
    'TechCrunch': ['.entry-content p', '.article-content p', '.wp-block-post-content p', 'article p'],
    'TechPowerUp': ['.article-content p', '#newsContent p', 'article p', '.fulltext p'],
    'TomsHardware': ['div#article-body p', '.article-body p', '#slice-container p', 'article p'],
    'SeekingAlpha_Tech': ["div[data-test-id='article-body'] p", '.paywall-full-content p', '.article-body p', 'article p'],
    'SemiAnalysis': ['.body p', '.post-content p', '.markup p', 'article p'],
    'SemiEngineering': ['.entry-content p', '.post-content p', 'article p'],
    'EETimes': ['.article-content p', '.post-content p', '.entry-content p', 'article p'],
    'ServeTheHome': ['.entry-content p', '.post-content p', 'article p'],
    'TheRegister_AI': ['#article-body p', '#body p', 'article p', "div[class*='article_body'] p"],
    'NVIDIA_Blog': ['.entry-content p', '.post-content p', '.blog-post p', 'article p'],
    'AWS_ML_Blog': ['.blog-post-content p', '.aws-blog-body p', '.entry-content p', 'article p'],
    'TechNews_TW': ['.indent p', '.entry-content p', '.article-content p', 'article p'],
    'DIGITIMES_TW': ['.article-body p', '.news_content p', '#content p', '.article-detail p', 'article p'],
    'TrendForce': ['.press-content p', '.article-content p', '.news-detail p', 'article p'],
    'ASML_News': ['.rich-text p', '.article-content p', '.body-content p', 'article p'],
    'HackerNews': ['.article-body p', '.post-content p', '.entry-content p', 'article p', '.content p'],
    'Lightwave': ['.article-body p', '.post-content p', '.entry-content p', 'article p', '.content p'],
    'FierceTelecom': ['.article-body p', '.post-content p', '.entry-content p', 'article p', '.content p'],
    'SEMI_News': ['.field-content p', '.node-content p', '.article-body p', 'article p'],
    'DataCenterKnowledge': ['.article-content p', '.post-content p', '.entry-content p', 'article p'],
    'TheElec_KR': ['.article-body p', '.view-content p', '.entry-content p', 'article p'],
    'DigiTimes_Asia': ['.article-body p', '.news_content p', '#content p', 'article p'],
    'FierceElectronics': ['.article-body p', '.post-content p', '.entry-content p', 'article p'],
    'SMM_Metal': ['.article-content p', '.news-content p', 'article p']
}

GENERIC_CONTENT_SELECTORS = ['article p', 'main p', '.content p', '.post-content p', '.entry-content p', '.article-body p', 'p']

URL_BLACKLIST = ['/media-library', '/contact-media', '/about-us', '/privacy', '/terms', '/news-subscription', '/news-alerts', '/share-buyback', '/login', '/register', '/subscribe', '/sitemap', '/advertis', '/cookie', '/legal', '/careers']

CONTENT_NOISE_KEYWORDS = ['cookie', 'privacy', 'subscribe', 'newsletter', 'sign up', 'log in', 'advertisement', 'javascript', '©', 'all rights reserved']

WEB_SCRAPE_SOURCES = [
    {'name': 'DIGITIMES_TW', 'url': 'https://www.digitimes.com.tw/', 'selector': "a[href*='/tech/'], a[href*='/news/'], h2 a, h3 a, .article-title a, .news-title a, .list-title a", 'title_attr': 'text', 'link_attr': 'href', 'category': '台湾供应鏈', 'fetcher': 'http'},
    {'name': 'TrendForce', 'url': 'https://www.trendforce.com/presscenter', 'selector': "a[href*='/presscenter/'], a[href*='/news/'], h3 a, .press-title a, .news-item a, article a, .list-item a", 'title_attr': 'text', 'link_attr': 'href', 'category': '半导体市场研究', 'fetcher': 'http'},
    {'name': 'ASML_News', 'url': 'https://www.asml.com/en/news', 'selector': "a[href*='/news/'], a.card-news__link, h3 a", 'title_attr': 'text', 'link_attr': 'href', 'category': '产业链动态', 'fetcher': 'http'},
    {'name': 'Lightwave', 'url': 'https://www.lightwaveonline.com/home', 'selector': "a.title-wrapper, a[href*='/home/article/'], a[href*='/fttx/'], a[href*='/optical-tech/'], h3 a", 'title_attr': 'text', 'link_attr': 'href', 'category': '光通信产业链', 'fetcher': 'http'},
    {'name': 'FierceTelecom', 'url': 'https://www.fierce-network.com/broadband', 'selector': "a[href*='/broadband/'], a[href*='/telecom/'], a[href*='/fiber/'], h2 a, h3 a, article a", 'title_attr': 'text', 'link_attr': 'href', 'category': '光通信产业链', 'fetcher': 'http'}
]
