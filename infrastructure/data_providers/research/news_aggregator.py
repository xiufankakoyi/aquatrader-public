"""News Aggregator - 金融新闻/每日简报聚合工具

功能：自动抓取当日金融新闻、行业动态、公告摘要、指数行情
数据源：新浪财经、同花顺快讯、东方财富7x24、搜狗微信文章、百度AI搜索、通达信指数
"""

from __future__ import annotations

import logging
import random
import re
import time
import uuid
from datetime import datetime
from typing import Any

import requests

from infrastructure.data_providers.provider_registry import ProviderRegistry

logger = logging.getLogger(__name__)

# 同一 API 调用间隔（秒）
_RATE_LIMIT_SECONDS = 0.3

# 通用请求头
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

# 通达信服务器列表，用于指数行情备用
_TDX_HOSTS = [
    ("218.6.170.47", 7709),
    ("123.125.108.14", 7709),
    ("180.153.18.170", 7709),
    ("60.191.117.167", 7709),
]
_TDX_HOST_INDEX = 0
_TDX_LAST_CALL: float = 0.0

# 主要指数配置：(名称, 市场代码, 指数代码)
_INDICES_CONFIG = [
    ("上证指数", 1, "000001"),
    ("深证成指", 0, "399001"),
    ("创业板指", 0, "399006"),
    ("科创50", 1, "000688"),
    ("上证50", 1, "000016"),
    ("沪深300", 1, "000300"),
]


def _rate_limit(last_call: list[float]) -> None:
    """限速：确保连续调用之间至少间隔 _RATE_LIMIT_SECONDS 秒。

    Args:
        last_call: 可变列表，[上次调用时间戳]，用于模拟引用传递
    """
    elapsed = time.time() - last_call[0]
    if elapsed < _RATE_LIMIT_SECONDS:
        time.sleep(_RATE_LIMIT_SECONDS - elapsed)
    last_call[0] = time.time()


class NewsAggregator:
    """金融新闻/每日简报聚合工具，支持多源采集与去重。"""

    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        """初始化新闻聚合器。

        Args:
            registry: 可选的数据提供者注册表，用于获取指数行情
        """
        self.registry = registry
        self._sina_last: list[float] = [0.0]
        self._ths_last: list[float] = [0.0]
        self._em_last: list[float] = [0.0]
        self._cninfo_last: list[float] = [0.0]
        self._wechat_last: list[float] = [0.0]
        self._baidu_last: list[float] = [0.0]

        # 百度 AI 搜索（日限100次，超限降级 DuckDuckGo）
        try:
            from infrastructure.data_providers.research.baidu_search_provider import BaiduSearchProvider
            self._baidu = BaiduSearchProvider()
        except Exception:
            self._baidu = None

        # arXiv 学术论文搜索
        try:
            from infrastructure.data_providers.research.arxiv_provider import ArxivProvider
            self._arxiv = ArxivProvider()
        except Exception:
            self._arxiv = None

    # ------------------------------------------------------------------
    # 新浪财经
    # ------------------------------------------------------------------

    def fetch_sina_news(self, pages: int = 3) -> list[dict[str, Any]]:
        """新浪财经多页采集。

        Args:
            pages: 采集页数，每页约 15 条

        Returns:
            新闻列表 [{title, url, time, digest}]
        """
        all_news: list[dict[str, Any]] = []
        # lid 配置：2509 财经要闻, 2516 全球财经, 2510 股票
        lids = [2509, 2516, 2510]

        for lid in lids:
            for page in range(1, pages + 1):
                _rate_limit(self._sina_last)
                try:
                    params = {
                        "pageid": "153",
                        "lid": str(lid),
                        "k": "",
                        "num": "15",
                        "page": str(page),
                    }
                    resp = requests.get(
                        "https://feed.mix.sina.com.cn/api/roll/get",
                        params=params,
                        headers=_HEADERS,
                        timeout=10,
                    )
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    items = data.get("result", {}).get("data", [])
                    for item in items:
                        all_news.append({
                            "title": item.get("title", ""),
                            "url": item.get("url", ""),
                            "time": item.get("ctime", ""),
                            "digest": item.get("digest", ""),
                        })
                except Exception as exc:
                    logger.warning("新浪财经采集失败 (lid=%s, page=%s): %s", lid, page, exc)
                    continue

        return all_news

    # ------------------------------------------------------------------
    # 同花顺快讯
    # ------------------------------------------------------------------

    def fetch_ths_headlines(self) -> list[dict[str, Any]]:
        """同花顺快讯/头条。

        Returns:
            新闻列表 [{title, digest, time, url}]
        """
        _rate_limit(self._ths_last)
        try:
            resp = requests.get(
                "https://news.10jqka.com.cn/tapp/news/push/stock",
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code != 200:
                logger.warning("同花顺快讯 HTTP %s", resp.status_code)
                return []

            data = resp.json()
            items = data.get("data", {}).get("list", [])
            headlines: list[dict[str, Any]] = []
            for item in items[:15]:
                ts = item.get("ctime", "")
                time_str = ""
                if ts and str(ts).isdigit():
                    time_str = datetime.fromtimestamp(int(ts)).strftime("%H:%M")
                headlines.append({
                    "title": item.get("title", ""),
                    "digest": item.get("digest", ""),
                    "time": time_str,
                    "url": item.get("shareUrl", item.get("url", "")),
                })
            return headlines
        except Exception as exc:
            logger.warning("同花顺快讯获取失败: %s", exc)
            return []

    # ------------------------------------------------------------------
    # 东方财富 7x24 快讯
    # ------------------------------------------------------------------

    def fetch_eastmoney_7x24(self, pages: int = 3) -> list[dict[str, Any]]:
        """东方财富7x24快讯多页采集。

        Args:
            pages: 采集页数

        Returns:
            新闻列表 [{title, url, time, digest}]
        """
        all_news: list[dict[str, Any]] = []
        # column=350 要闻, column=351 7x24快讯
        columns = [350, 351]

        for column in columns:
            for page in range(1, pages + 1):
                _rate_limit(self._em_last)
                try:
                    # req_trace 参数为必填，用于追踪请求
                    req_trace = uuid.uuid4().hex[:16]
                    params = {
                        "client": "web",
                        "biz": "web_news_col",
                        "column": str(column),
                        "order": "1",
                        "needInteractData": "0",
                        "page_index": str(page),
                        "page_size": "20",
                        "req_trace": req_trace,
                    }
                    resp = requests.get(
                        "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns",
                        params=params,
                        headers=_HEADERS,
                        timeout=10,
                    )
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    items = data.get("data", {}).get("list", [])
                    if not items:
                        continue
                    for item in items:
                        # 东方财富时间字段可能是时间戳或字符串
                        em_time = item.get("showtime", item.get("ctime", ""))
                        if isinstance(em_time, (int, float)) and em_time > 1e9:
                            em_time = datetime.fromtimestamp(int(em_time)).strftime("%H:%M")
                        all_news.append({
                            "title": item.get("title", ""),
                            "url": item.get("url", item.get("news_url", "")),
                            "time": str(em_time),
                            "digest": item.get("digest", item.get("content", "")),
                        })
                except Exception as exc:
                    logger.warning("东方财富7x24采集失败 (column=%s, page=%s): %s", column, page, exc)
                    continue

        return all_news

    # ------------------------------------------------------------------
    # 巨潮公告搜索
    # ------------------------------------------------------------------

    def fetch_cninfo_announcements(
        self, keyword: str, count: int = 10
    ) -> list[dict[str, Any]]:
        """巨潮公告搜索（POST 请求）。

        Args:
            keyword: 搜索关键词
            count: 返回条数上限

        Returns:
            公告列表 [{title, date, code, name, pdf_url}]
        """
        _rate_limit(self._cninfo_last)
        try:
            payload = {
                "searchkey": keyword,
                "sdate": "",
                "edate": "",
                "isfulltext": "false",
                "sortName": "pubdate",
                "sortType": "desc",
                "pageNum": 1,
                "pageSize": count,
                "column": "szse",
                "tabName": "fulltext",
                "category": "",
                "seDate": "",
            }
            headers = {
                "User-Agent": _HEADERS["User-Agent"],
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "http://www.cninfo.com.cn/",
            }
            resp = requests.post(
                "http://www.cninfo.com.cn/new/fulltextSearch/full",
                data=payload,
                headers=headers,
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning("巨潮公告 HTTP %s", resp.status_code)
                return []

            data = resp.json()
            announcements: list[dict[str, Any]] = []
            items = data.get("announcements", [])
            for item in items[:count]:
                # 拼接公告 PDF 链接
                adj_url = item.get("adjunctUrl", "")
                pdf_url = f"http://static.cninfo.com.cn/{adj_url}" if adj_url else ""
                announcements.append({
                    "title": item.get("announcementTitle", ""),
                    "date": item.get("announcementTime", ""),
                    "code": item.get("secCode", ""),
                    "name": item.get("secName", ""),
                    "pdf_url": pdf_url,
                })
            return announcements
        except Exception as exc:
            logger.warning("巨潮公告搜索失败 (keyword=%s): %s", keyword, exc)
            return []

    # ------------------------------------------------------------------
    # 搜狗微信文章搜索
    # ------------------------------------------------------------------

    # UA 随机池，避免固定 UA 被封
    _WECHAT_UA_POOL: list[str] = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
        "Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 Version/17.2 Mobile Safari/604.1",
    ]

    def _get_sogou_cookie(self) -> str:
        """访问搜狗子域获取 Cookie，用于后续微信搜索。

        Returns:
            Cookie 字符串
        """
        try:
            session = requests.Session()
            session.trust_env = False
            resp = session.get(
                "https://v.sogou.com/v?ie=utf8&query=&p=40030600",
                headers={
                    "User-Agent": random.choice(self._WECHAT_UA_POOL),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Encoding": "identity",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
                timeout=10,
            )
            cookies = []
            for key, value in resp.cookies.items():
                cookies.append(f"{key}={value}")
            return "; ".join(cookies)
        except Exception:
            return ""

    def _resolve_wechat_real_url(self, sogou_url: str, cookie_str: str = "") -> str:
        """将搜狗中间链接解析为微信文章真实链接。

        搜狗返回的链接是中间跳转页，尝试从跳转页 HTML 中提取
        mp.weixin.qq.com 的真实 URL。

        Args:
            sogou_url: 搜狗中间链接
            cookie_str: Cookie 字符串

        Returns:
            真实微信文章 URL，解析失败则返回原链接
        """
        if "weixin.sogou.com" not in sogou_url:
            return sogou_url

        try:
            session = requests.Session()
            session.trust_env = False
            headers = {
                "User-Agent": random.choice(self._WECHAT_UA_POOL),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "identity",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://weixin.sogou.com/",
            }
            if cookie_str:
                headers["Cookie"] = cookie_str

            resp = session.get(sogou_url, headers=headers, timeout=10, allow_redirects=False)

            # 302 重定向
            if 300 <= resp.status_code < 400:
                location = resp.headers.get("Location", "")
                if "mp.weixin.qq.com" in location:
                    return location

            # 200 时从 HTML 中提取跳转 URL
            if resp.status_code == 200:
                html = resp.text
                # 方式1: url += 'https://mp.weixin.qq.com/...' 拼接模式
                url_parts = re.findall(r"url\s*\+=\s*'([^']*)'", html)
                url_parts += re.findall(r'url\s*\+=\s*"([^"]*)"', html)
                if url_parts:
                    joined = "".join(url_parts)
                    if "mp.weixin.qq.com" in joined:
                        return joined

                # 方式2: location.href = '...'
                js_match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", html)
                if js_match and "mp.weixin.qq.com" in js_match.group(1):
                    return js_match.group(1)

                # 方式3: meta refresh
                meta_match = re.search(
                    r'<meta[^>]*http-equiv=["\']refresh["\'][^>]*content=["\']\d+;\s*url=([^"\']+)["\']',
                    html,
                    re.IGNORECASE,
                )
                if meta_match and "mp.weixin.qq.com" in meta_match.group(1):
                    return meta_match.group(1)

        except Exception:
            pass

        return sogou_url

    def fetch_wechat_articles(
        self,
        keyword: str = "A股 股市",
        count: int = 10,
        resolve_url: bool = False,
    ) -> list[dict[str, Any]]:
        """搜狗微信文章搜索（type=2 为文章搜索）。

        增强：UA 随机池、Cookie 预获取、多页搜索、真实 URL 解析。

        Args:
            keyword: 搜索关键词
            count: 返回条数上限（最大50）
            resolve_url: 是否解析搜狗中间链接为微信真实链接

        Returns:
            文章列表 [{title, url, digest, account, time}]
        """
        _rate_limit(self._wechat_last)
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.warning("beautifulsoup4 未安装，无法搜索微信文章")
            return []

        try:
            import random as _random
        except ImportError:
            _random = random

        count = min(count, 50)
        session = requests.Session()
        session.trust_env = False

        # 步骤1: 预获取搜狗 Cookie
        cookie_str = self._get_sogou_cookie()

        # 步骤2: 多页搜索
        all_articles: list[dict[str, Any]] = []
        pages_needed = max(1, (count + 9) // 10)

        for page in range(1, pages_needed + 1):
            if len(all_articles) >= count:
                break

            _rate_limit(self._wechat_last)
            try:
                params = {
                    "type": 2,
                    "query": keyword,
                    "s_from": "input",
                    "_sug_": "n",
                    "page": page,
                    "ie": "utf8",
                }
                headers = {
                    "User-Agent": _random.choice(self._WECHAT_UA_POOL),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Encoding": "identity",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Referer": "https://weixin.sogou.com/",
                }
                if cookie_str:
                    headers["Cookie"] = cookie_str

                resp = session.get(
                    "https://weixin.sogou.com/weixin",
                    params=params,
                    headers=headers,
                    timeout=15,
                )
                if resp.status_code != 200:
                    logger.warning("搜狗微信搜索 HTTP %s (page=%s)", resp.status_code, page)
                    break

                # 反爬检测
                if "antispider" in resp.text or "验证" in resp.text:
                    logger.warning("搜狗微信搜索触发反爬验证 (page=%s)", page)
                    break

                # 更新 cookie
                for key, value in resp.cookies.items():
                    cookie_str = cookie_str + f"; {key}={value}" if cookie_str else f"{key}={value}"

                soup = BeautifulSoup(resp.text, "html.parser")
                items = soup.select(".news-list li")
                if not items:
                    break

                for li in items:
                    if len(all_articles) >= count:
                        break

                    # 标题与链接
                    title_tag = li.select_one("h3 a")
                    title = title_tag.get_text(strip=True) if title_tag else ""
                    link = title_tag.get("href", "") if title_tag else ""
                    if link and not link.startswith("http"):
                        link = "https://weixin.sogou.com" + link

                    # 摘要（优先 .txt-info，其次 p）
                    p_tag = li.select_one("p.txt-info") or li.select_one("p")
                    digest = p_tag.get_text(strip=True) if p_tag else ""

                    # 公众号名称
                    account_tag = li.select_one(".all-time-y2") or li.select_one("a.account")
                    account = account_tag.get_text(strip=True) if account_tag else ""

                    # 时间戳（从 .s2 的 HTML 中提取 timeConvert 参数）
                    time_text = ""
                    sp_tag = li.select_one(".s2")
                    if sp_tag:
                        sp_html = str(sp_tag)
                        ts_match = re.search(r"timeConvert\('(\d+)'\)", sp_html)
                        if ts_match:
                            try:
                                ts = int(ts_match.group(1))
                                time_text = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                            except (ValueError, OSError):
                                pass

                    if title:
                        all_articles.append({
                            "title": title,
                            "url": link,
                            "digest": digest,
                            "account": account,
                            "time": time_text,
                        })

                # 翻页延迟
                if page < pages_needed:
                    time.sleep(0.5 + random.random())

            except Exception as exc:
                logger.warning("搜狗微信搜索失败 (keyword=%s, page=%s): %s", keyword, page, exc)
                break

        # 步骤3: 可选 - 解析真实 URL
        if resolve_url and all_articles:
            for article in all_articles:
                url = article.get("url", "")
                if "weixin.sogou.com" in url:
                    real_url = self._resolve_wechat_real_url(url, cookie_str)
                    article["url"] = real_url
                    # 翻页延迟避免过快
                    time.sleep(0.5 + random.random())

        return all_articles

    # ------------------------------------------------------------------
    # 百度 AI 搜索 / DuckDuckGo 降级
    # ------------------------------------------------------------------

    def search_web(
        self,
        query: str,
        count: int = 10,
        freshness: str | None = None,
    ) -> list[dict[str, Any]]:
        """百度 AI 搜索，日限100次，超限降级 DuckDuckGo。

        Args:
            query: 搜索关键词
            count: 返回结果数（1-50）
            freshness: 时间范围 "pd"/"pw"/"pm"/"py" 或 "YYYY-MM-DDtoYYYY-MM-DD"

        Returns:
            搜索结果列表 [{title, url, snippet, source, time}]
        """
        if self._baidu and self._baidu.is_available():
            _rate_limit(self._baidu_last)
            return self._baidu.search(query, count, freshness)

        # 无百度 Key，直接降级 DuckDuckGo
        if self._baidu:
            _rate_limit(self._baidu_last)
            return self._baidu._duckduckgo_search(query, count)

        return []

    def search_stock_news(
        self,
        keyword: str,
        count: int = 10,
    ) -> list[dict[str, Any]]:
        """搜索股票相关新闻（百度优先，降级 DuckDuckGo）。

        Args:
            keyword: 股票名称或关键词
            count: 返回条数

        Returns:
            新闻列表 [{title, url, snippet, source, time}]
        """
        return self.search_web(query=f"{keyword} 股票 最新消息", count=count, freshness="pw")

    # ------------------------------------------------------------------
    # arXiv 学术论文搜索
    # ------------------------------------------------------------------

    def search_arxiv(
        self,
        keyword: str = "",
        count: int = 10,
        scope: str = "finance",
    ) -> list[dict[str, Any]]:
        """搜索 arXiv 学术论文。

        Args:
            keyword: 搜索关键词
            count: 返回结果数
            scope: 搜索范围 "finance"(量化金融) / "ml_finance"(ML+金融) / "all"(全领域)

        Returns:
            论文列表 [{title, url, pdf_url, abstract, authors, published, arxiv_id}]
        """
        if self._arxiv is None:
            return []

        if scope == "finance":
            return self._arxiv.search_finance_papers(keyword=keyword, count=count)
        elif scope == "ml_finance":
            return self._arxiv.search_ml_finance_papers(keyword=keyword, count=count)
        else:
            query = f'ti:"{keyword}"' if keyword else "cat:q-fin"
            return self._arxiv.search_papers(query=query, count=count, sort_by="submittedDate")

    # ------------------------------------------------------------------
    # 指数行情
    # ------------------------------------------------------------------

    def _get_index_data_via_registry(self) -> list[dict[str, Any]]:
        """通过 TdxProvider 批量获取指数行情（1次连接查6个指数）。

        Returns:
            指数行情列表 [{name, price, change_pct}]
        """
        if self.registry is None:
            return []
        try:
            tdx = self.registry.providers.get("tdx")
            if tdx and tdx.is_available():
                result = tdx.get_index_quotes()
                if result:
                    return result
            # 降级：通过 registry 全市场行情筛选指数
            index_codes = [code for _, _, code in _INDICES_CONFIG]
            df = self.registry.get_realtime_quotes(symbols=index_codes)
            if df is None or df.empty:
                return []
            code_to_name = {code: name for name, _, code in _INDICES_CONFIG}
            result: list[dict[str, Any]] = []
            symbol_col = None
            for col in ("symbol", "ts_code", "stock_code", "code"):
                if col in df.columns:
                    symbol_col = col
                    break
            if symbol_col is None:
                return []
            for code in index_codes:
                row = df[df[symbol_col].astype(str).str.replace(".", "").str[:6] == code]
                if row.empty:
                    continue
                r = row.iloc[0]
                name = code_to_name.get(code, str(r.get("stock_name", r.get("name", ""))))
                price = float(r.get("price", r.get("close", 0)) or 0)
                pct = float(r.get("pct_chg", r.get("change_pct", 0)) or 0)
                result.append({"name": name, "price": price, "change_pct": pct})
            return result
        except Exception as exc:
            logger.warning("通过 registry 获取指数行情失败: %s", exc)
            return []

    def _get_index_data_via_tdx(self) -> list[dict[str, Any]]:
        """通过通达信获取指数行情（备用方案）。

        Returns:
            指数行情列表 [{name, price, change_pct}]
        """
        global _TDX_HOST_INDEX, _TDX_LAST_CALL

        # 限速
        elapsed = time.time() - _TDX_LAST_CALL
        if elapsed < 0.5:
            time.sleep(0.5 - elapsed)
        _TDX_LAST_CALL = time.time()

        try:
            from pytdx.hq import TdxHq_API

            for _ in range(len(_TDX_HOSTS)):
                ip, port = _TDX_HOSTS[_TDX_HOST_INDEX % len(_TDX_HOSTS)]
                _TDX_HOST_INDEX += 1
                api = TdxHq_API()
                try:
                    api.connect(ip, port, time_out=5)
                    codes = [(mkt, code) for _, mkt, code in _INDICES_CONFIG]
                    quotes = api.get_security_quotes(codes)
                    api.disconnect()
                    if not quotes:
                        break
                    result: list[dict[str, Any]] = []
                    for i, (name, _, _) in enumerate(_INDICES_CONFIG):
                        if i < len(quotes) and quotes[i]:
                            q = quotes[i]
                            last_close = q.get("last_close", 0)
                            price = q.get("price", 0)
                            change_pct = round(
                                (price - last_close) / last_close * 100, 2
                            ) if last_close else 0
                            result.append({
                                "name": name,
                                "price": price,
                                "change_pct": change_pct,
                            })
                    return result
                except Exception:
                    try:
                        api.disconnect()
                    except Exception:
                        pass
                    continue
        except ImportError:
            logger.warning("pytdx 未安装，无法通过通达信获取指数行情")
        except Exception as exc:
            logger.warning("通达信获取指数行情失败: %s", exc)
        return []

    # ------------------------------------------------------------------
    # 每日简报
    # ------------------------------------------------------------------

    def daily_briefing(self) -> dict[str, Any]:
        """生成每日简报，包含指数行情与新闻头条。

        Returns:
            {date, time, type, market: [{name, price, change_pct}], headlines: [...]}
        """
        now = datetime.now()

        # 获取指数行情：优先 registry，备用 TDX
        market_data = self._get_index_data_via_registry()
        if not market_data:
            market_data = self._get_index_data_via_tdx()

        # 聚合新闻头条
        headlines: list[dict[str, Any]] = []
        try:
            headlines.extend(self.fetch_sina_news(pages=1))
        except Exception:
            pass
        try:
            headlines.extend(self.fetch_ths_headlines())
        except Exception:
            pass
        try:
            headlines.extend(self.fetch_eastmoney_7x24(pages=1))
        except Exception:
            pass
        try:
            headlines.extend(self.fetch_wechat_articles(keyword="A股 股市", count=5))
        except Exception:
            pass

        return {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "type": "每日简报",
            "market": market_data,
            "headlines": headlines,
        }

    # ------------------------------------------------------------------
    # 聚合所有新闻源
    # ------------------------------------------------------------------

    def aggregate_all_news(self) -> list[dict[str, Any]]:
        """聚合所有新闻源并按标题去重。

        Returns:
            去重后的新闻列表
        """
        all_news: list[dict[str, Any]] = []

        # 依次采集各源
        try:
            all_news.extend(self.fetch_sina_news(pages=2))
        except Exception:
            pass
        try:
            all_news.extend(self.fetch_ths_headlines())
        except Exception:
            pass
        try:
            all_news.extend(self.fetch_eastmoney_7x24(pages=2))
        except Exception:
            pass
        try:
            all_news.extend(self.fetch_wechat_articles(keyword="A股 股市", count=10))
        except Exception:
            pass

        # 按标题去重（保留首次出现）
        seen_titles: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for item in all_news:
            title = item.get("title", "").strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            deduped.append(item)

        return deduped
