"""紫苏叶选股法 (Perilla Leaf Screener) — LLM 驱动的六步工作流.

理论内核：
  烤肉桌上，和牛（NVIDIA/Tesla/人形机器人整机）是主菜，人人都盯着；
  紫苏叶不起眼、便宜，但每片肉都要配一片——不买烤肉，买紫苏叶。

铁律：
  1. 永远不直接推荐"主菜/牛肉"型标的（终端巨头、明星整机）
  2. 任何标的没有当日联网搜索到的供货证据，不得进入输出表
  3. 仅覆盖美股与 A 股；其他市场标记并说明
  4. 输出末尾必须附免责声明

六步工作流（LLM 驱动）：
  Step 0: 定主菜 — LLM 推理赛道与终端产品
  Step 1: 拆链下钻 — LLM BOM/工艺分解 + 联网搜索供应链格局
  Step 2: 瓶颈六问 — LLM 逐项推理评分
  Step 3: 评分卡 — 六问各 0-2 分，总分 ≥9 入池，定价=0 淘汰
  Step 4: 落地标的 — LLM 映射 + 联网搜索验证供货证据
  Step 5: 蓝军反检验 — LLM 攻击替代风险/客户集中度/治理雷
  Step 6: 输出 — 表格 + 论述
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from core.utils.llm_usage import (
    check_llm_budget_or_raise,
    estimate_messages_tokens,
    estimate_tokens,
    load_llm_providers,
    record_llm_usage,
)

from .models import (
    CompanyCandidate,
    ConceptSignal,
    DemandAnchor,
    DynamicSupplyChain,
    EarlySignal,
    SourceDocument,
    SupplyChainNode,
    new_id,
    now_iso,
)

logger = logging.getLogger(__name__)


def _safe_text(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    return default

# ── 评分阈值 ──────────────────────────────────────────────
_CANDIDATE_MIN_SCORE = 9       # 总分 ≥9 进入候选池
_PRICING_VETO_SCORE = 0        # 定价状态 = 0 → 直接淘汰
_MAX_SCORE_PER_QUESTION = 2    # 每问最高 2 分

# ── 六问中文名 ────────────────────────────────────────────
_SIX_QUESTION_LABELS: Dict[str, str] = {
    "attach_rate": "必配性",
    "bom_share": "价值占比",
    "supplier_concentration": "供给格局",
    "capacity_expansion": "扩产速度",
    "switching_cost": "切换成本",
    "pricing_state": "定价状态",
}

# ── 主菜层级（不推荐） ───────────────────────────────────
_MAIN_DISH_LAYERS: frozenset[str] = frozenset({"terminal", "module"})

# ── 紫苏叶高发区 ──────────────────────────────────────────
_PERILLA_LAYERS: frozenset[str] = frozenset({
    "material", "equipment", "testing", "consumable", "component",
})

# ── 联网搜索查询模板 ──────────────────────────────────────
_SUPPLY_CHAIN_SEARCH_QUERIES = [
    "{topic} 供应链 瓶颈 供应商",
    "{topic} 产业链 上游 材料 独家",
    "{topic} 核心供应商 产能 认证",
]
_TICKER_EVIDENCE_SEARCH_QUERIES = [
    "{company} {node} 供应商 供货 认证",
    "{company} {topic} 产业链 量产 出货",
    "{company} 客户 产能 公告",
]
_NODE_BOTTLENECK_SEARCH_QUERIES = [
    "{node} 供应商 集中度 独家",
    "{node} 产能 扩产 良率 认证周期",
    "{node} 切换成本 门槛 瓶颈",
]

# ── Batch 验证配置 ──────────────────────────────────────
_BATCH_SIZE = 5            # 每批验证公司数
_MAX_CONCURRENCY = 3       # 最大并发批次数
_PROMPT_VERSION = "v2"     # prompt 版本号（缓存失效用）

# ── 规则预分类关键词 ────────────────────────────────────
_CONFIRMED_KEYWORDS = re.compile(r"供货|认证|客户|订单|量产|出货|交付|采购|签约")
_PROBABLE_KEYWORDS = re.compile(r"布局|研发|试产|小批量|送样|验证中")
_CONCEPT_KEYWORDS = re.compile(r"概念|题材|板块|炒作|关注|有望|预期|或可")

# ── 低质量证据链接过滤规则 ──────────────────────────────
_LOW_QUALITY_URL_PATTERNS: List[str] = [
    r"baike\.baidu\.com",            # 百度百科词条
    r"zdic\.net",                     # 汉典
    r"hanyuguoxue\.com",              # 汉语国学
    r"douban\.com",                   # 豆瓣
    r"zhidao\.baidu\.com",            # 百度知道
    r"wenku\.baidu\.com",             # 百度文库
    r"tieba\.baidu\.com",             # 百度贴吧
    r"baike\.so\.com",                # 360百科
    r"baike\.sogou\.com",             # 搜狗百科
    r"wikipedia\.org",                # 维基百科（非供货证据）
    r"cngold\.org",                   # 黄金网
    r"chembk\.com",                   # 化工百科
    r"dxy\.com",                      # 丁香园
    r"site\.ip138\.com",              # IP查询
    r"sj\.qq\.com",                   # 应用宝
    r"consumer\.huawei\.com",         # 华为消费者
    r"beijing\.gov\.cn",              # 北京政府网
    r"shanghai\.gov\.cn",             # 上海政府网
    r"avdioes\.com",                  # 无关网站
    r"zhihu\.com/question",           # 知乎问答
    r"zhuanlan\.zhihu\.com/p/\d+$",   # 知乎专栏
    r"chagushici\.com",               # 查古诗词（字典页）
    r"hgcha\.com/zidian",             # 字典页
    r"hancibao\.com/zi/",             # 汉词宝字典
    r"gushici\.net/\d",               # 古诗词网字典
    r"zidian\.gushici\.net",          # 古诗词网字典
    r"v\.qq\.com",                    # 腾讯视频
    r"tencent\.com",                  # 腾讯官网
    r"amap\.com",                     # 高德地图
    r"visitbeijing\.com\.cn",         # 北京旅游
    r"yn\.gov\.cn",                   # 云南政府网
    r"m\.163\.com/dy/article",        # 网易娱乐
    r"sohu\.com/a/\d+",               # 搜狐新闻（非专业来源）
    r"163\.com/dy/article",           # 网易娱乐
    r"news\.qq\.com/rain",            # 腾讯新闻（非专业来源）
]


# ══════════════════════════════════════════════════════════
# 数据模型
# ══════════════════════════════════════════════════════════

@dataclass
class PerillaLeafCandidate:
    """紫苏叶候选标的.

    Attributes:
        candidate_id: 唯一标识。
        company_name: 公司名称。
        symbol: 证券代码（如 300308.SZ, NVDA）。
        market: 市场（A_SHARE / US / HK / JP / OTHER）。
        market_warning: 非核心市场提示。
        perilla_node: 紫苏叶环节名称。
        main_dish: 对应的主菜（终端产品）。
        six_question_scores: 六问评分（各 0-2）。
        total_score: 六问总分（0-12）。
        pricing_vetoed: 是否因定价状态=0被淘汰。
        bottleneck_logic: 一句话瓶颈逻辑。
        stress_test: 压力测试答案。
        stress_test_passed: 压力测试是否通过。
        red_team_risks: 蓝军反检验风险列表。
        risk_level: 综合风险等级（low/medium/high）。
        evidence_links: 供货证据链接列表。
        evidence_level: 证据等级。
        is_fake_perilla: 是否为伪紫苏叶（无供货证据）。
        fake_reason: 伪紫苏叶原因。
        in_pool: 是否进入候选池。
        six_question_reasoning: 六问评分推理过程。
    """

    candidate_id: str = ""
    company_name: str = ""
    symbol: str = ""
    market: str = "UNKNOWN"
    market_warning: str = ""
    perilla_node: str = ""
    main_dish: str = ""
    six_question_scores: Dict[str, int] = field(default_factory=lambda: {
        "attach_rate": 0,
        "bom_share": 0,
        "supplier_concentration": 0,
        "capacity_expansion": 0,
        "switching_cost": 0,
        "pricing_state": 0,
    })
    total_score: int = 0
    pricing_vetoed: bool = False
    bottleneck_logic: str = ""
    stress_test: str = ""
    stress_test_passed: bool = False
    red_team_risks: List[str] = field(default_factory=list)
    risk_level: str = "medium"
    evidence_links: List[str] = field(default_factory=list)
    evidence_level: str = "INSUFFICIENT_EVIDENCE"
    is_fake_perilla: bool = False
    fake_reason: str = ""
    in_pool: bool = False
    six_question_reasoning: Dict[str, str] = field(default_factory=dict)
    company_classification: str = ""  # 主链锚点/相关/概念蹭热/早期信号/紫苏叶


@dataclass
class PerillaLeafReport:
    """紫苏叶选股法输出报告.

    Attributes:
        run_id: 运行标识。
        topic: 输入主题。
        main_dish: 主菜定义。
        chain_summary: 产业链摘要。
        candidates: 候选标的列表。
        top_picks: 最有信心的 1-3 个标的。
        eliminated: 被淘汰的标的（含伪紫苏叶）。
        disclaimer: 免责声明。
        generated_at: 生成时间。
        llm_used: 是否使用了 LLM。
        web_search_used: 是否使用了联网搜索。
    """

    run_id: str = ""
    topic: str = ""
    main_dish: str = ""
    chain_summary: str = ""
    candidates: List[PerillaLeafCandidate] = field(default_factory=list)
    top_picks: List[PerillaLeafCandidate] = field(default_factory=list)
    eliminated: List[PerillaLeafCandidate] = field(default_factory=list)
    disclaimer: str = ""
    generated_at: str = ""
    llm_used: bool = False
    web_search_used: bool = False
    report_status: str = "COMPLETED"  # PRELIMINARY_PERILLA_DISCOVERY | DEEP_REVIEW_READY | COMPLETED


# ══════════════════════════════════════════════════════════
# PerillaLeafScreener — LLM 驱动的六步工作流
# ══════════════════════════════════════════════════════════

class PerillaLeafScreener:
    """紫苏叶选股法执行器 — LLM 驱动的六步工作流.

    严格按六步工作流执行，不可跳步。
    每步使用 LLM 推理 + 联网搜索验证，确保分析质量。
    LLM 不可用时降级到规则模式。
    """

    def __init__(self, progress_callback: Optional[Callable[[str], None]] = None) -> None:
        """初始化紫苏叶选股器.

        Args:
            progress_callback: 进度回调函数，接收进度消息字符串。
                设置后每步执行时会调用，用于实时显示进度。
        """
        self._llm_api_base: str = ""
        self._llm_model_name: str = ""
        self._llm_api_key: str = ""
        self._llm_available: bool = False
        self._llm_providers = []
        self._search_available: bool = False
        self._progress_callback = progress_callback
        self._verification_cache: Dict[str, Dict[str, Any]] = {}  # cache_key → result
        self._init_llm_config()
        self._init_search()

    def _progress(self, msg: str) -> None:
        """输出进度消息（日志 + 回调）.

        Args:
            msg: 进度消息。
        """
        logger.info(msg)
        if self._progress_callback:
            self._progress_callback(msg)

    def _init_llm_config(self) -> None:
        """从 Secrets 读取 LLM 配置."""
        try:
            from config.secrets import Secrets
            self._llm_api_base = getattr(Secrets, "LLM_API_BASE", "")
            self._llm_model_name = getattr(Secrets, "LLM_MODEL_NAME", "")
            self._llm_api_key = getattr(Secrets, "LLM_API_KEY", "")
        except ImportError:
            logger.warning("config/secrets.py 未找到，LLM 不可用")
        except Exception as exc:
            logger.warning("读取 LLM 配置失败: %s", exc)

        if self._llm_api_base and self._llm_model_name:
            self._llm_available = True
            logger.info("紫苏叶 LLM 已启用: base=%s, model=%s",
                        self._llm_api_base, self._llm_model_name)
        else:
            logger.info("紫苏叶 LLM 不可用，将使用规则降级模式")
        self._llm_providers = load_llm_providers(
            self._llm_api_base,
            self._llm_api_key,
            self._llm_model_name,
        )
        self._llm_available = bool(self._llm_providers)

    def _init_search(self) -> None:
        """初始化联网搜索.

        优先使用 SearXNG，不可用时降级到 DuckDuckGo/Bing。
        中文查询自动使用 Bing（DuckDuckGo 对中文支持差）。
        """
        self._search_method: str = "none"  # "searxng" | "duckduckgo" | "bing" | "none"
        self._search = None
        self._ddgs = None

        # 1. 尝试 SearXNG
        try:
            from config.secrets import Secrets
            searxng_url = getattr(Secrets, "SEARXNG_BASE_URL", "")
        except ImportError:
            searxng_url = ""

        if searxng_url:
            try:
                from .search_discovery import SearchDiscovery
                self._search = SearchDiscovery(
                    searxng_base_url=searxng_url,
                    max_results=5,
                    enabled=True,
                )
                self._search_available = True
                self._search_method = "searxng"
                logger.info("紫苏叶联网搜索已启用: SearXNG %s", searxng_url)
                return
            except Exception as exc:
                logger.warning("SearXNG 初始化失败: %s", exc)

        # 2. 尝试 DuckDuckGo API（英文查询可用）
        try:
            from duckduckgo_search import DDGS
            self._ddgs = DDGS()
            self._search_available = True
            self._search_method = "duckduckgo"
            logger.info("紫苏叶联网搜索已启用: DuckDuckGo + Bing (SearXNG 不可用)")
        except ImportError:
            # 3. 降级到纯 Bing HTML 搜索
            self._search_available = True
            self._search_method = "bing"
            logger.info("紫苏叶联网搜索已启用: Bing HTML (SearXNG 和 ddgs 均不可用)")
        except Exception as exc:
            self._search_available = True
            self._search_method = "bing"
            logger.info("紫苏叶联网搜索已启用: Bing HTML (ddgs 初始化失败: %s)", exc)

    # ══════════════════════════════════════════════════════
    # LLM 调用
    # ══════════════════════════════════════════════════════

    def _call_llm(self, prompt: str, system_prompt: str = "") -> Optional[str]:
        """Call configured providers with fallback and rough token accounting."""
        if not self._llm_available:
            return None
        try:
            import openai
        except ImportError:
            logger.warning("openai package is not installed; cannot call LLM")
            return None

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        prompt_tokens = estimate_messages_tokens(messages)
        max_tokens = 4096
        try:
            check_llm_budget_or_raise(prompt_tokens, max_tokens, source="PerillaLeafScreener._call_llm")
        except RuntimeError as exc:
            logger.warning("%s", exc)
            return None

        for provider in self._llm_providers:
            try:
                client = openai.OpenAI(
                    base_url=provider.api_base,
                    api_key=provider.api_key or "dummy",
                )
                response = client.chat.completions.create(
                    model=provider.model_name,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=max_tokens,
                    stream=False,
                )
                content = response.choices[0].message.content or ""
                record_llm_usage(
                    source="PerillaLeafScreener._call_llm",
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=estimate_tokens(content),
                    success=True,
                )
                return content
            except Exception as exc:
                record_llm_usage(
                    source="PerillaLeafScreener._call_llm",
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=0,
                    success=False,
                    error=str(exc),
                )
                logger.warning("LLM provider failed (%s): %s", provider.name, exc)
        return None

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        """从 LLM 响应中提取 JSON.

        Args:
            text: LLM 响应文本。

        Returns:
            解析后的字典，失败返回 None。
        """
        if not isinstance(text, str) or not text.strip():
            return None

        # 尝试从 markdown 代码块中提取
        pattern = r"```(?:json)?\s*\n?(.*?)\n?\s*```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                value = json.loads(match.group(1).strip())
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                pass

        # 尝试直接查找 JSON 对象
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                value = json.loads(text[start:end + 1])
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                pass

        # 尝试查找 JSON 数组
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                value = json.loads(text[start:end + 1])
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                pass

        return None

    # ══════════════════════════════════════════════════════
    # 联网搜索
    # ══════════════════════════════════════════════════════

    def _web_search(self, query: str) -> str:
        """执行联网搜索，返回结果文本.

        中文查询自动使用 Bing（DuckDuckGo 对中文支持差）。
        英文查询优先 DuckDuckGo API。

        Args:
            query: 搜索查询。

        Returns:
            搜索结果文本（标题 + 摘要），失败返回空字符串。
        """
        query = _safe_text(query).strip()
        if not query:
            return ""

        # 检测是否包含中文字符
        has_chinese = bool(re.search(r'[\u4e00-\u9fff]', query))

        # 1. SearXNG（支持所有语言）
        if self._search_method == "searxng" and self._search:
            try:
                docs = self._search.search(query)
                if docs:
                    parts = []
                    for doc in docs[:5]:
                        title = _safe_text(getattr(doc, "title", ""))
                        content = _safe_text(getattr(doc, "content_text", ""))
                        url = _safe_text(getattr(doc, "url", ""))
                        if title or content:
                            parts.append(f"【{title}】{content[:200]}")
                            if url:
                                parts.append(f"来源: {url}")
                    result = "\n".join(parts)
                    if result.strip():
                        return result
            except Exception as exc:
                logger.debug("SearXNG 搜索失败 '%s': %s", query, exc)

        # 2. 中文查询 → Bing（DuckDuckGo 对中文支持差）
        if has_chinese:
            result = self._web_search_bing(query)
            if result.strip():
                return result

        # 3. 英文查询 → DuckDuckGo API
        if self._search_method == "duckduckgo" and self._ddgs:
            try:
                results = self._ddgs.text(query, max_results=5)
                if results:
                    parts = []
                    for r in results:
                        if not isinstance(r, dict):
                            continue
                        title = _safe_text(r.get("title"))
                        body = _safe_text(r.get("body"))
                        href = _safe_text(r.get("href"))
                        if title or body:
                            parts.append(f"【{title}】{body[:200]}")
                            if href:
                                parts.append(f"来源: {href}")
                    result = "\n".join(parts)
                    if result.strip():
                        return result
            except Exception as exc:
                logger.debug("DuckDuckGo API 搜索失败 '%s': %s", query, exc)

        # 4. 兜底：Bing HTML
        return self._web_search_bing(query)

    def _web_search_bing(self, query: str) -> str:
        """使用 requests + bs4 搜索 Bing.

        Bing 对中文查询支持良好，是中文供应链搜索的首选。

        Args:
            query: 搜索查询。

        Returns:
            搜索结果文本。
        """
        query = _safe_text(query).strip()
        if not query:
            return ""
        try:
            import requests as req
            from bs4 import BeautifulSoup
            from urllib.parse import quote_plus

            url = f"https://cn.bing.com/search?q={quote_plus(query)}"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            resp = req.get(url, headers=headers, timeout=15)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            parts = []
            # Bing 搜索结果在 #b_results > li.b_algo 中
            for result in soup.select("#b_results > li.b_algo")[:5]:
                title_el = result.select_one("h2 a")
                snippet_el = result.select_one(".b_caption p") or result.select_one("p")
                title = title_el.get_text(strip=True) if title_el else ""
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                href = title_el.get("href", "") if title_el else ""
                if title or snippet:
                    parts.append(f"【{title}】{snippet[:200]}")
                    if href:
                        parts.append(f"来源: {href}")
            return "\n".join(parts)
        except Exception as exc:
            logger.debug("Bing 搜索失败 '%s': %s", query, exc)
            return ""

    def _web_search_duckduckgo_html(self, query: str) -> str:
        """使用 requests + bs4 搜索 DuckDuckGo HTML 版本.

        Args:
            query: 搜索查询。

        Returns:
            搜索结果文本。
        """
        query = _safe_text(query).strip()
        if not query:
            return ""
        try:
            import requests as req
            from bs4 import BeautifulSoup
            from urllib.parse import quote_plus

            url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
            resp = req.get(url, headers=headers, timeout=15)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            parts = []
            for result in soup.select(".result")[:5]:
                title_el = result.select_one(".result__title a")
                snippet_el = result.select_one(".result__snippet")
                title = title_el.get_text(strip=True) if title_el else ""
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                href = title_el.get("href", "") if title_el else ""
                if title or snippet:
                    parts.append(f"【{title}】{snippet[:200]}")
                    if href:
                        parts.append(f"来源: {href}")
            return "\n".join(parts)
        except Exception as exc:
            logger.debug("DuckDuckGo HTML 搜索失败 '%s': %s", query, exc)
            return ""

    def _search_supply_chain_info(self, topic: str) -> str:
        """搜索供应链格局信息.

        Args:
            topic: 主题。

        Returns:
            搜索结果文本。
        """
        topic = _safe_text(topic).strip()
        if not topic:
            return ""
        all_results = []
        for tpl in _SUPPLY_CHAIN_SEARCH_QUERIES:
            query = tpl.replace("{topic}", topic)
            result = self._web_search(query)
            if result:
                all_results.append(result)
        return "\n\n".join(all_results)

    def _search_node_bottleneck_info(self, node_name: str) -> str:
        """搜索节点瓶颈信息.

        Args:
            node_name: 节点名称。

        Returns:
            搜索结果文本。
        """
        node_name = _safe_text(node_name).strip()
        if not node_name:
            return ""
        all_results = []
        for tpl in _NODE_BOTTLENECK_SEARCH_QUERIES:
            query = tpl.replace("{node}", node_name)
            result = self._web_search(query)
            if result:
                all_results.append(result)
        return "\n\n".join(all_results)

    def _search_ticker_evidence(
        self, company_name: str, node_name: str, topic: str,
    ) -> Tuple[str, List[str]]:
        """搜索标的供货证据.

        Args:
            company_name: 公司名称。
            node_name: 节点名称。
            topic: 主题。

        Returns:
            (证据文本, 证据链接列表) 二元组。
        """
        company_name = _safe_text(company_name).strip()
        node_name = _safe_text(node_name).strip()
        topic = _safe_text(topic).strip()
        if not company_name and not node_name and not topic:
            return "", []
        all_text = []
        all_links = []

        for tpl in _TICKER_EVIDENCE_SEARCH_QUERIES:
            query = tpl.replace("{company}", company_name).replace(
                "{node}", node_name,
            ).replace("{topic}", topic)
            result = self._web_search(query)
            if result:
                all_text.append(result)

        # 提取链接：根据搜索方法使用不同策略
        if self._search_method == "searxng" and self._search:
            for tpl in _TICKER_EVIDENCE_SEARCH_QUERIES[:2]:
                query = tpl.replace("{company}", company_name).replace(
                    "{node}", node_name,
                ).replace("{topic}", topic)
                try:
                    docs = self._search.search(query)
                    for doc in docs[:3]:
                        url = _safe_text(getattr(doc, "url", ""))
                        if url and url not in all_links:
                            all_links.append(url)
                except Exception:
                    pass
        elif self._search_method == "duckduckgo" and self._ddgs:
            # 英文查询用 DDG API
            for tpl in _TICKER_EVIDENCE_SEARCH_QUERIES[:2]:
                query = tpl.replace("{company}", company_name).replace(
                    "{node}", node_name,
                ).replace("{topic}", topic)
                has_chinese = bool(re.search(r'[\u4e00-\u9fff]', query))
                if not has_chinese:
                    try:
                        results = self._ddgs.text(query, max_results=3)
                        for r in (results or []):
                            if not isinstance(r, dict):
                                continue
                            href = _safe_text(r.get("href"))
                            if href and href not in all_links:
                                all_links.append(href)
                    except Exception:
                        pass
                else:
                    links = self._extract_links_from_bing(query)
                    all_links.extend(l for l in links if l not in all_links)
        else:
            # Bing HTML
            for tpl in _TICKER_EVIDENCE_SEARCH_QUERIES[:2]:
                query = tpl.replace("{company}", company_name).replace(
                    "{node}", node_name,
                ).replace("{topic}", topic)
                links = self._extract_links_from_bing(query)
                all_links.extend(l for l in links if l not in all_links)

        # 从搜索文本中提取已有链接
        for text in all_text:
            for line in text.split("\n"):
                if line.startswith("来源: ") and len(line) > 6:
                    link = line[4:].strip()
                    if link not in all_links:
                        all_links.append(link)

        # 过滤低质量链接
        all_links = self._filter_evidence_links(all_links)

        return "\n\n".join(all_text), all_links

    def _extract_links_from_bing(self, query: str) -> List[str]:
        """从 Bing 搜索结果中提取链接.

        Args:
            query: 搜索查询。

        Returns:
            URL 列表。
        """
        query = _safe_text(query).strip()
        if not query:
            return []
        links = []
        try:
            import requests as req
            from bs4 import BeautifulSoup
            from urllib.parse import quote_plus

            url = f"https://cn.bing.com/search?q={quote_plus(query)}"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            resp = req.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for result in soup.select("#b_results > li.b_algo")[:3]:
                title_el = result.select_one("h2 a")
                if title_el:
                    href = title_el.get("href", "")
                    if href and href not in links:
                        links.append(href)
        except Exception as exc:
            logger.debug("Bing 链接提取失败: %s", exc)
        return links

    # ══════════════════════════════════════════════════════
    # Step 0: 定主菜
    # ══════════════════════════════════════════════════════
    # URL 质量过滤
    # ══════════════════════════════════════════════════════

    @staticmethod
    def _is_low_quality_url(url: str) -> bool:
        """判断URL是否为低质量链接（字典页/百科页/无关页）.

        Args:
            url: 待检查的URL。

        Returns:
            True 表示低质量，应过滤。
        """
        url = _safe_text(url).strip()
        if not url:
            return True
        for pattern in _LOW_QUALITY_URL_PATTERNS:
            if re.search(pattern, url):
                return True
        return False

    @staticmethod
    def _filter_evidence_links(links: List[str]) -> List[str]:
        """过滤低质量证据链接，保留专业来源.

        Args:
            links: 原始链接列表。

        Returns:
            过滤后的链接列表。
        """
        if not isinstance(links, (list, tuple)):
            return []
        output: list[str] = []
        seen: set[str] = set()
        for link in links:
            value = _safe_text(link).strip()
            if value and value not in seen and not PerillaLeafScreener._is_low_quality_url(value):
                output.append(value)
                seen.add(value)
        return output

    # ══════════════════════════════════════════════════════
    # Step 0: 定主菜
    # ══════════════════════════════════════════════════════

    def step0_define_main_dish(self, topic: str) -> str:
        """Step 0: 确定主菜（终端产品/赛道）.

        使用 LLM 推理主菜定义，降级时使用简单规则。

        Args:
            topic: 输入主题。

        Returns:
            主菜描述字符串。
        """
        topic = _safe_text(topic).strip()
        if not topic:
            return ""
        if not self._llm_available:
            return self._step0_fallback(topic)

        prompt = f"""你是一位供应链分析专家。根据以下主题，确定"主菜"（终端产品/明星整机）。

主菜定义规则：
- AI 供应链 → GPU/ASIC 服务器、CPO/光互连、HBM、AI 手机/PC
- 物理 AI 供应链 → 自动驾驶域控、激光雷达、工业机器人、低空飞行器
- 具身智能 → 人形机器人整机（Optimus/Figure/宇树等）、灵巧手、机器狗

主菜是"和牛"——人人都盯着的大东西。紫苏叶是配菜——不起眼但每片肉都要配一片。

主题：{topic}

严格输出 JSON：
{{
  "main_dish": "主菜描述（包含具体终端产品名称）",
  "main_dish_category": "AI供应链|物理AI供应链|具身智能|其他",
  "terminal_products": ["终端产品1", "终端产品2"]
}}"""

        result = self._call_llm(prompt)
        parsed = self._extract_json(result) if result else None
        if parsed and "main_dish" in parsed:
            return parsed["main_dish"]

        return self._step0_fallback(topic)

    @staticmethod
    def _step0_fallback(topic: str) -> str:
        """Step 0 降级：简单规则确定主菜."""
        topic = _safe_text(topic).strip()
        if not topic:
            return ""
        topic_lower = topic.lower()
        category_map = {
            "光互连": "CPO/光互连模块（AI服务器光互连）",
            "cpo": "CPO共封装光学模块（AI服务器光互连）",
            "hbm": "HBM高带宽内存（AI服务器内存）",
            "gpu": "GPU/ASIC服务器（AI算力基础设施）",
            "aipc": "AI PC（AI终端设备）",
            "机器人": "人形机器人整机（具身智能终端）",
            "人形机器人": "人形机器人整机（具身智能终端）",
            "自动驾驶": "自动驾驶域控制器（物理AI终端）",
            "激光雷达": "激光雷达（自动驾驶传感器）",
            "低空": "低空飞行器/eVTOL（物理AI终端）",
        }
        for key, desc in category_map.items():
            if key in topic_lower:
                return desc
        return f"{topic} 终端产品"

    # ══════════════════════════════════════════════════════
    # Step 1: 拆链下钻
    # ══════════════════════════════════════════════════════

    def step1_drill_down_chain(
        self, main_dish: str, topic: str,
    ) -> List[Dict[str, Any]]:
        """Step 1: BOM/工艺流程分解，逐层下钻.

        使用 LLM 推理供应链结构，联网搜索补充供应链格局信息。
        每一层都问："这一层里，哪个东西量小价低但缺了不行？"

        Args:
            main_dish: 主菜描述。
            topic: 输入主题。

        Returns:
            供应链节点列表（字典格式）。
        """
        main_dish = _safe_text(main_dish).strip()
        topic = _safe_text(topic).strip()
        if not topic and not main_dish:
            return []
        # 联网搜索供应链信息
        search_evidence = self._search_supply_chain_info(topic)

        if not self._llm_available:
            return self._step1_fallback(topic)

        search_section = ""
        if search_evidence:
            search_section = f"""
## 联网搜索到的供应链信息
{search_evidence[:2000]}
"""

        prompt = f"""你是一位供应链瓶颈分析专家，正在执行"紫苏叶选股法"的拆链下钻。

## 任务
对主菜做 BOM/工艺流程分解，逐层下钻：终端 → 模组 → 器件 → 材料/设备/测试/耗材。

每一层都问："这一层里，哪个东西量小价低但缺了不行？"

关键规则：
- 材料、设备、测试、耗材四类是紫苏叶高发区
- 整机和明星模组是"牛肉"，标记 is_perilla_candidate=false
- 必须展开到 material/equipment/testing/consumable 层级
- 每个节点要具体到实际产品/材料名称，不要用"核心材料"这种泛称
{search_section}
## 输入
主菜：{main_dish}
主题：{topic}

## 输出要求
严格输出 JSON：
{{
  "chain_nodes": [
    {{
      "name": "具体节点名称（如'磷化铟衬底'而非'核心材料'）",
      "layer": "terminal|module|component|material|equipment|testing|consumable",
      "description": "该节点在供应链中的角色描述",
      "is_perilla_candidate": true,
      "perilla_reason": "为什么这是紫苏叶候选：量小价低但缺了不行"
    }},
    {{
      "name": "GPU服务器",
      "layer": "terminal",
      "description": "终端产品",
      "is_perilla_candidate": false,
      "perilla_reason": "主菜/牛肉型，人人都盯着"
    }}
  ]
}}

要求：
1. 至少展开 8-15 个节点
2. material/equipment/testing/consumable 层至少各 1-2 个
3. 节点名称必须具体（如"磷化铟衬底"而非"光芯片材料"）
4. 只基于行业常识和搜索证据推理，不要编造不存在的环节"""

        result = self._call_llm(prompt)
        parsed = self._extract_json(result) if result else None

        if parsed and "chain_nodes" in parsed:
            nodes = [node for node in parsed["chain_nodes"] if isinstance(node, dict)]
            if len(nodes) >= 3:
                logger.info("Step 1 LLM 拆链: %d 个节点", len(nodes))
                return nodes

        logger.warning("Step 1 LLM 拆链失败，降级到规则模式")
        return self._step1_fallback(topic)

    @staticmethod
    def _step1_fallback(topic: str) -> List[Dict[str, Any]]:
        """Step 1 降级：通用供应链模板."""
        return [
            {"name": f"{topic}终端产品", "layer": "terminal",
             "description": f"{topic}的终端产品", "is_perilla_candidate": False,
             "perilla_reason": "主菜/牛肉型"},
            {"name": f"{topic}核心模组", "layer": "module",
             "description": f"{topic}的核心模组", "is_perilla_candidate": False,
             "perilla_reason": "明星模组，关注度较高"},
            {"name": f"{topic}关键器件", "layer": "component",
             "description": f"{topic}的关键器件", "is_perilla_candidate": True,
             "perilla_reason": "器件层可能存在瓶颈"},
            {"name": f"{topic}核心材料", "layer": "material",
             "description": f"{topic}的核心材料", "is_perilla_candidate": True,
             "perilla_reason": "材料层紫苏叶高发区：量小价低但缺了不行"},
            {"name": f"{topic}专用设备", "layer": "equipment",
             "description": f"{topic}的专用设备", "is_perilla_candidate": True,
             "perilla_reason": "设备层紫苏叶高发区：交期长、know-how壁垒"},
            {"name": f"{topic}测试/检测", "layer": "testing",
             "description": f"{topic}的测试检测环节", "is_perilla_candidate": True,
             "perilla_reason": "测试层紫苏叶高发区：每单位必测"},
        ]

    # ══════════════════════════════════════════════════════
    # Step 2: 瓶颈六问
    # ══════════════════════════════════════════════════════

    def step2_six_questions(
        self,
        node: Dict[str, Any],
        main_dish: str,
        topic: str,
    ) -> Dict[str, Any]:
        """Step 2: 对候选环节回答六问（LLM 推理 + 联网搜索）.

        Args:
            node: 供应链节点字典。
            main_dish: 主菜描述。
            topic: 主题。

        Returns:
            包含 scores, reasoning, total_score, in_pool, pricing_vetoed 的字典。
        """
        node = node if isinstance(node, dict) else {}
        node_name = _safe_text(node.get("name"))
        node_layer = _safe_text(node.get("layer"), "unknown") or "unknown"
        node_desc = _safe_text(node.get("description"))
        perilla_reason = _safe_text(node.get("perilla_reason"))
        main_dish = _safe_text(main_dish)
        topic = _safe_text(topic)

        # 联网搜索节点瓶颈信息
        search_evidence = self._search_node_bottleneck_info(node_name)

        if not self._llm_available:
            return self._step2_fallback(node)

        search_section = ""
        if search_evidence:
            search_section = f"""
## 联网搜索到的瓶颈信息
{search_evidence[:1500]}
"""

        prompt = f"""你是一位供应链瓶颈分析专家，正在执行"紫苏叶选股法"的瓶颈六问评估。

## 任务
对以下供应链节点进行六问评估。每问 0-2 分，必须给出评分理由。

### 六问定义
1. **必配性**（attach_rate）：主菜出货一单位，它是否必然消耗/配套？attach rate 接近 100%？
2. **价值占比**（bom_share）：占下游 BOM <5%？占比低 → 下游对它涨价不敏感，降本时不会先砍它
3. **供给格局**（supplier_concentration）：全球/国内是否 ≤3 家有效供应商？是否独家认证？
4. **扩产速度**（capacity_expansion）：产能是否难以快速扩张（工艺 know-how、设备交期、良率爬坡 ≥1 年）？
5. **切换成本**（switching_cost）：换供应商是否需要重新认证 6 个月以上？是否绑定大客户 roadmap？
6. **定价状态**（pricing_state）：近 6 个月涨幅是否尚未反映瓶颈逻辑？

### 评分规则
- 0 = 否 / 无证据支持
- 1 = 部分 / 有迹象但不确定
- 2 = 强 / 有确凿证据

### 关键规则
- 第 6 问（定价状态）= 0 → 直接淘汰（已被定价的紫苏叶就是牛肉）
- 总分 ≥9 进入候选池
{search_section}
## 输入
主菜：{main_dish}
节点：{node_name}（{node_layer}层）
描述：{node_desc}
紫苏叶理由：{perilla_reason}

## 输出要求
严格输出 JSON：
{{
  "scores": {{
    "attach_rate": 0-2,
    "bom_share": 0-2,
    "supplier_concentration": 0-2,
    "capacity_expansion": 0-2,
    "switching_cost": 0-2,
    "pricing_state": 0-2
  }},
  "reasoning": {{
    "attach_rate": "评分理由（1-2句话）",
    "bom_share": "评分理由",
    "supplier_concentration": "评分理由",
    "capacity_expansion": "评分理由",
    "switching_cost": "评分理由",
    "pricing_state": "评分理由"
  }},
  "total_score": 0-12,
  "in_pool": true/false,
  "pricing_vetoed": true/false,
  "bottleneck_logic": "一句话瓶颈逻辑（如'磷化铟衬底全球仅3家供应商，认证周期>1年，占光模块BOM<3%'）"
}}"""

        result = self._call_llm(prompt)
        parsed = self._extract_json(result) if result else None

        if parsed and "scores" in parsed:
            scores = parsed["scores"] if isinstance(parsed["scores"], dict) else {}
            # 验证分数范围
            for key in list(_SIX_QUESTION_LABELS.keys()):
                scores[key] = max(0, min(2, _safe_int(scores.get(key), 0)))

            total = sum(scores.values())
            pricing_vetoed = scores.get("pricing_state", 0) == 0
            in_pool = total >= _CANDIDATE_MIN_SCORE and not pricing_vetoed

            parsed["total_score"] = total
            parsed["in_pool"] = in_pool
            parsed["pricing_vetoed"] = pricing_vetoed

            if "bottleneck_logic" not in parsed or not parsed["bottleneck_logic"]:
                parsed["bottleneck_logic"] = f"{node_name}在{main_dish}供应链中存在潜在瓶颈"

            logger.info(
                "Step 2 LLM 六问: %s → %d/12, in_pool=%s",
                node_name, total, in_pool,
            )
            return parsed

        logger.warning("Step 2 LLM 六问失败 (%s)，降级到规则模式", node_name)
        return self._step2_fallback(node)

    @staticmethod
    def _step2_fallback(node: Dict[str, Any]) -> Dict[str, Any]:
        """Step 2 降级：基于层级的默认评分."""
        node = node if isinstance(node, dict) else {}
        layer = _safe_text(node.get("layer"), "unknown") or "unknown"

        layer_defaults = {
            "material": {"attach_rate": 2, "bom_share": 2, "supplier_concentration": 1,
                         "capacity_expansion": 1, "switching_cost": 1, "pricing_state": 1},
            "equipment": {"attach_rate": 2, "bom_share": 1, "supplier_concentration": 2,
                          "capacity_expansion": 2, "switching_cost": 2, "pricing_state": 1},
            "testing": {"attach_rate": 2, "bom_share": 2, "supplier_concentration": 1,
                        "capacity_expansion": 1, "switching_cost": 1, "pricing_state": 1},
            "consumable": {"attach_rate": 2, "bom_share": 2, "supplier_concentration": 1,
                           "capacity_expansion": 1, "switching_cost": 1, "pricing_state": 1},
            "component": {"attach_rate": 2, "bom_share": 1, "supplier_concentration": 1,
                          "capacity_expansion": 1, "switching_cost": 1, "pricing_state": 1},
        }

        fallback_defaults = {"attach_rate": 1, "bom_share": 1, "supplier_concentration": 1,
                             "capacity_expansion": 1, "switching_cost": 1, "pricing_state": 1}

        scores = layer_defaults.get(layer, fallback_defaults)
        total = sum(scores.values())
        pricing_vetoed = scores.get("pricing_state", 0) == 0
        in_pool = total >= _CANDIDATE_MIN_SCORE and not pricing_vetoed

        return {
            "scores": scores,
            "reasoning": {k: f"基于{layer}层级默认评分（LLM不可用）" for k in scores},
            "total_score": total,
            "in_pool": in_pool,
            "pricing_vetoed": pricing_vetoed,
            "bottleneck_logic": f"{node.get('name', '')}在供应链中存在潜在瓶颈（规则降级评估）",
        }

    # ══════════════════════════════════════════════════════
    # Step 3: 评分卡（纯逻辑，不需要 LLM）
    # ══════════════════════════════════════════════════════

    def step3_scoring_card(
        self, scores: Dict[str, int],
    ) -> Tuple[int, bool]:
        """Step 3: 评分卡判定.

        规则：
        - 六问各 0-2 分，总分 ≥9 进入候选池
        - 第 6 问（定价状态）= 0 → 直接淘汰

        Args:
            scores: 六问评分。

        Returns:
            (总分, 是否被定价否决)。
        """
        scores = scores if isinstance(scores, dict) else {}
        normalized = {
            key: max(0, min(_MAX_SCORE_PER_QUESTION, _safe_int(scores.get(key), 0)))
            for key in _SIX_QUESTION_LABELS
        }
        total = sum(normalized.values())
        if normalized.get("pricing_state", 0) == _PRICING_VETO_SCORE:
            return total, True
        return total, False

    # ══════════════════════════════════════════════════════
    # 合并评估：一次 LLM 调用完成六问+评分+标的映射+分类
    # ══════════════════════════════════════════════════════

    @staticmethod
    def _normalize_market(market_raw: str, symbol: str = "") -> str:
        """标准化市场标识.

        Args:
            market_raw: LLM 返回的市场标识。
            symbol: 证券代码（辅助判断）。

        Returns:
            标准化后的市场标识（A_SHARE/US/HK/JP/OTHER）。
        """
        m = _safe_text(market_raw).upper().strip()
        symbol = _safe_text(symbol).upper().strip()
        if m in ("A_SHARE", "ASHARE", "A股", "A股市场"):
            return "A_SHARE"
        if m in ("US", "美股", "美国"):
            return "US"
        if m in ("HK", "港股", "香港"):
            return "HK"
        if m in ("JP", "日股", "日本"):
            return "JP"
        if m in ("TW", "台股", "台湾"):
            return "TW"
        # 从代码推断
        if symbol:
            if re.match(r"^\d{6}\.(SH|SZ|BJ)$", symbol):
                return "A_SHARE"
            if re.match(r"^\d{6}$", symbol):
                return "A_SHARE"
            if re.match(r"^[A-Z]{1,5}$", symbol):
                return "US"
        return "OTHER"

    def evaluate_node_full(
        self,
        node: Dict[str, Any],
        main_dish: str,
        topic: str,
        preset_signals: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], List[PerillaLeafCandidate]]:
        """合并 Step 2+3+4：一次 LLM 调用完成六问评分+标的映射+分类.

        加速关键：从每个节点 3-4 次 LLM 调用降到 1 次。

        Args:
            node: 供应链节点字典。
            main_dish: 主菜描述。
            topic: 主题。
            preset_signals: 预置信号（来自 JSON 文件），可直接作为证据。

        Returns:
            (scores_result, candidates) 元组。
        """
        node = node if isinstance(node, dict) else {}
        node_name = _safe_text(node.get("name"))
        node_layer = _safe_text(node.get("layer"), "unknown") or "unknown"
        node_desc = _safe_text(node.get("description"))
        perilla_reason = _safe_text(node.get("perilla_reason"))
        main_dish = _safe_text(main_dish)
        topic = _safe_text(topic)

        if not self._llm_available:
            scores_result = self._step2_fallback(node)
            return scores_result, self._step4_fallback(node, main_dish, scores_result)

        # 并行搜索：瓶颈信息 + 供应商信息
        bottleneck_search = self._search_node_bottleneck_info(node_name)
        supplier_search = self._search_ticker_evidence("", node_name, topic)
        supplier_search_text = supplier_search[0] if supplier_search else ""

        # 预置信号
        preset_section = ""
        if isinstance(preset_signals, (list, tuple)) and preset_signals:
            signal_lines = []
            for s in preset_signals:
                if not isinstance(s, dict):
                    continue
                signal_lines.append(
                    f"- {_safe_text(s.get('company_name'))}({_safe_text(s.get('symbol'))}): "
                    f"{_safe_text(s.get('claim'))} | 证据: {_safe_text(s.get('evidence_text'))} | "
                    f"可信度: {_safe_text(s.get('credibility_hint'))}"
                )
            preset_section = f"""
## 预置信号（来自本地研究数据，可信度较高）
{chr(10).join(signal_lines)}
"""

        search_section = ""
        search_parts = []
        if bottleneck_search:
            search_parts.append(f"### 瓶颈信息\n{bottleneck_search[:1000]}")
        if supplier_search_text:
            search_parts.append(f"### 供应商搜索\n{supplier_search_text[:1000]}")
        if search_parts:
            search_section = f"""
## 联网搜索结果
{chr(10).join(search_parts)}
"""

        prompt = f"""你是一位供应链瓶颈分析专家，正在执行"紫苏叶选股法"的完整评估。

## 任务
对以下供应链节点一次性完成：六问评分 + 标的映射 + 公司分类。

### 铁律
1. 永远不推荐"主菜/牛肉"型标的（终端巨头、明星整机）
2. 仅覆盖美股与 A 股；港股等其他市场标记
3. A 股"蹭概念"极多，无实锤供货证据的标记为概念蹭热
4. 优先基于搜索证据和预置信号映射标的

### 六问定义
1. **必配性**（attach_rate）：主菜出货一单位，它是否必然消耗/配套？
2. **价值占比**（bom_share）：占下游 BOM <5%？
3. **供给格局**（supplier_concentration）：全球/国内是否 ≤3 家有效供应商？
4. **扩产速度**（capacity_expansion）：产能是否难以快速扩张（≥1 年）？
5. **切换成本**（switching_cost）：换供应商是否需重新认证 6 个月以上？
6. **定价状态**（pricing_state）：近 6 个月涨幅是否尚未反映瓶颈逻辑？

### 评分规则
0 = 否, 1 = 部分, 2 = 强。定价状态=0 → 直接淘汰。

### 公司分类定义
- **紫苏叶**：量小价低但缺了不行，供给集中，切换成本高，尚未被定价（六问总分≥9，定价>0）
- **主链锚点**：供应链核心环节的大公司，市值大、关注度高，是"牛肉"不是紫苏叶
- **相关**：参与供应链但非瓶颈环节，或供给充分可替代
- **概念蹭热**：声称参与但无实锤供货证据，A 股常见
- **早期信号**：有技术储备/小批量供货，但尚未进入主流供应链

### A 股代码格式
上海: 6XXXXX.SH, 深圳: 0XXXXX.SZ/3XXXXX.SZ, 北交所: XXXXXX.BJ
{preset_section}{search_section}
## 输入
主菜：{main_dish}
节点：{node_name}（{node_layer}层）
描述：{node_desc}
紫苏叶理由：{perilla_reason}

## 输出要求
严格输出 JSON：
{{
  "scores": {{
    "attach_rate": 0-2,
    "bom_share": 0-2,
    "supplier_concentration": 0-2,
    "capacity_expansion": 0-2,
    "switching_cost": 0-2,
    "pricing_state": 0-2
  }},
  "reasoning": {{
    "attach_rate": "理由",
    "bom_share": "理由",
    "supplier_concentration": "理由",
    "capacity_expansion": "理由",
    "switching_cost": "理由",
    "pricing_state": "理由"
  }},
  "total_score": 0-12,
  "in_pool": true/false,
  "pricing_vetoed": true/false,
  "bottleneck_logic": "一句话瓶颈逻辑",
  "tickers": [
    {{
      "company_name": "公司名",
      "symbol": "代码（如 300308.SZ 或 NVDA）",
      "market": "A_SHARE|US|HK|OTHER",
      "classification": "紫苏叶|主链锚点|相关|概念蹭热|早期信号",
      "supply_evidence": "供货证据描述",
      "evidence_source": "证据来源",
      "stress_test": "如果这家公司明天停产，链条上谁会停摆？",
      "stress_test_passed": true/false,
      "is_fake_perilla": false,
      "fake_reason": ""
    }}
  ]
}}

要求：
1. 每个环节映射 1-3 家最相关的公司
2. 优先映射搜索证据和预置信号中有明确供货关系的公司
3. 对每家公司给出分类标签（紫苏叶/主链锚点/相关/概念蹭热/早期信号）
4. 不确定的公司标记 is_fake_perilla=true 并说明原因"""

        result = self._call_llm(prompt)
        parsed = self._extract_json(result) if result else None

        if not parsed or "scores" not in parsed:
            logger.warning("合并评估 LLM 失败 (%s)，降级", node_name)
            scores_result = self._step2_fallback(node)
            return scores_result, self._step4_fallback(
                node, main_dish, scores_result, reason="合并评估LLM调用失败",
            )

        # 解析六问评分
        scores = parsed["scores"] if isinstance(parsed["scores"], dict) else {}
        for key in list(_SIX_QUESTION_LABELS.keys()):
            scores[key] = max(0, min(2, _safe_int(scores.get(key), 0)))

        total = sum(scores.values())
        pricing_vetoed = scores.get("pricing_state", 0) == 0
        in_pool = total >= _CANDIDATE_MIN_SCORE and not pricing_vetoed

        scores_result = {
            "scores": scores,
            "reasoning": parsed.get("reasoning", {}),
            "total_score": total,
            "in_pool": in_pool,
            "pricing_vetoed": pricing_vetoed,
            "bottleneck_logic": parsed.get("bottleneck_logic", ""),
        }

        # 解析标的
        tickers_data = parsed.get("tickers", [])
        if not isinstance(tickers_data, (list, tuple)):
            tickers_data = []
        candidates = self._parse_tickers_to_candidates(
            tickers_data, node, main_dish, scores_result, topic,
        )

        return scores_result, candidates

    def _parse_tickers_to_candidates(
        self,
        tickers_data: List[Dict[str, Any]],
        node: Dict[str, Any],
        main_dish: str,
        scores_result: Dict[str, Any],
        topic: str,
    ) -> List[PerillaLeafCandidate]:
        """将 LLM 返回的标的 JSON 解析为 PerillaLeafCandidate 列表.

        四大加速策略：
        1. 规则预分类：跳过不需要 LLM 的公司
        2. Batch 验证：同节点多家公司合并为1次 LLM 调用
        3. 并发执行：batch 级并发（max_concurrency=3）
        4. 缓存前置：相同公司+节点+证据不重复调用 LLM

        Args:
            tickers_data: LLM 返回的标的列表。
            node: 供应链节点字典。
            main_dish: 主菜描述。
            scores_result: 评分结果。
            topic: 主题。

        Returns:
            候选标的列表。
        """
        node = node if isinstance(node, dict) else {}
        scores_result = scores_result if isinstance(scores_result, dict) else {}
        node_name = _safe_text(node.get("name"))
        total_score = _safe_int(scores_result.get("total_score"), 0)
        pricing_vetoed = _safe_bool(scores_result.get("pricing_vetoed"))
        main_dish = _safe_text(main_dish)
        topic = _safe_text(topic)

        # ── Phase 1: 规则预分类 ──
        rule_classified: List[PerillaLeafCandidate] = []  # 规则直接判定的
        need_search: List[Dict[str, Any]] = []             # 需要搜索验证的

        for t in tickers_data if isinstance(tickers_data, (list, tuple)) else []:
            if not isinstance(t, dict):
                continue
            company_name = _safe_text(t.get("company_name"))
            symbol = _safe_text(t.get("symbol"))
            market_raw = _safe_text(t.get("market"), "OTHER") or "OTHER"
            classification = _safe_text(t.get("classification"), "相关") or "相关"
            supply_evidence = _safe_text(t.get("supply_evidence"))
            evidence_source = _safe_text(t.get("evidence_source"))
            stress_test = _safe_text(t.get("stress_test"))
            stress_passed = _safe_bool(t.get("stress_test_passed"), bool(stress_test))
            is_fake = _safe_bool(t.get("is_fake_perilla"))
            fake_reason = _safe_text(t.get("fake_reason"))

            market = self._normalize_market(market_raw, symbol)

            # 铁律：主菜层级
            if node.get("layer") in _MAIN_DISH_LAYERS:
                rule_classified.append(self._make_candidate(
                    company_name, symbol, market, node_name, main_dish,
                    scores_result, stress_test, stress_passed,
                    evidence_links=[], evidence_level="INSUFFICIENT_EVIDENCE",
                    is_fake=True, fake_reason="主菜/牛肉型标的，违反铁律",
                    classification="主链锚点",
                ))
                continue

            # 规则1: 非核心市场 → 直接标记
            if market not in ("A_SHARE", "US"):
                rule_classified.append(self._make_candidate(
                    company_name, symbol, market, node_name, main_dish,
                    scores_result, stress_test, stress_passed,
                    evidence_links=[], evidence_level="INSUFFICIENT_EVIDENCE",
                    is_fake=False, fake_reason=f"非A股/美股市场({market})",
                    classification="主链锚点" if classification == "主链锚点" else "相关",
                ))
                continue

            # 规则2: LLM 已标记为伪紫苏叶
            if is_fake:
                rule_classified.append(self._make_candidate(
                    company_name, symbol, market, node_name, main_dish,
                    scores_result, stress_test, stress_passed,
                    evidence_links=[], evidence_level="INSUFFICIENT_EVIDENCE",
                    is_fake=True, fake_reason=fake_reason or "LLM判定无供货证据",
                    classification="概念蹭热",
                ))
                continue

            # 规则3: 未上市/无代码
            if not symbol or symbol in ("N/A", "未上市", ""):
                rule_classified.append(self._make_candidate(
                    company_name, symbol or "N/A", market, node_name, main_dish,
                    scores_result, stress_test, stress_passed,
                    evidence_links=[], evidence_level="INSUFFICIENT_EVIDENCE",
                    is_fake=True, fake_reason="未上市或无证券代码",
                    classification="概念蹭热",
                ))
                continue

            # 规则4: 基于证据文本关键词预分类
            evidence_text = f"{supply_evidence} {evidence_source}".strip()
            if evidence_text:
                if _CONFIRMED_KEYWORDS.search(evidence_text):
                    # 有确认关键词 → PROBABLE_SUPPLIER，仍需搜索验证
                    t["_rule_evidence_level"] = "PROBABLE_SUPPLIER"
                elif _CONCEPT_KEYWORDS.search(evidence_text) and not _PROBABLE_KEYWORDS.search(evidence_text):
                    # 只有概念关键词，无研发/试产关键词 → 概念蹭热
                    rule_classified.append(self._make_candidate(
                        company_name, symbol, market, node_name, main_dish,
                        scores_result, stress_test, stress_passed,
                        evidence_links=[], evidence_level="CONCEPT_CLAIM_ONLY",
                        is_fake=True, fake_reason="证据仅含概念性描述，无实锤供货证据",
                        classification="概念蹭热",
                    ))
                    continue

            # 需要搜索验证
            need_search.append(t)

        # ── Phase 2: 搜索证据（并行） ──
        search_results: Dict[str, Tuple[str, List[str]]] = {}  # company → (text, links)
        if need_search and self._search_available:
            def _search_one(t: Dict[str, Any]) -> Tuple[str, Tuple[str, List[str]]]:
                cn = _safe_text(t.get("company_name"))
                ev_text, ev_links = self._search_ticker_evidence(cn, node_name, topic)
                return cn, (ev_text, ev_links)

            with ThreadPoolExecutor(max_workers=_MAX_CONCURRENCY) as executor:
                futures = {executor.submit(_search_one, t): t for t in need_search}
                for future in as_completed(futures):
                    try:
                        cn, result = future.result()
                        search_results[cn] = result
                    except Exception as e:
                        logger.warning("搜索失败: %s", e)

        # ── Phase 3: 规则二次分类 + 缓存检查 ──
        need_llm: List[Dict[str, Any]] = []  # 真正需要 LLM 的

        for t in need_search:
            company_name = _safe_text(t.get("company_name"))
            ev_text, ev_links = search_results.get(company_name, ("", []))
            ev_text = _safe_text(ev_text)
            filtered_links = self._filter_evidence_links(ev_links)
            t["_search_text"] = ev_text
            t["_search_links"] = filtered_links

            # 规则5: 搜索无结果 → 直接标记
            if not ev_text.strip():
                rule_classified.append(self._make_candidate(
                    company_name, t.get("symbol", ""), self._normalize_market(t.get("market", "OTHER"), t.get("symbol", "")),
                    node_name, main_dish, scores_result,
                    _safe_text(t.get("stress_test")), _safe_bool(t.get("stress_test_passed"), True),
                    evidence_links=filtered_links, evidence_level="INSUFFICIENT_EVIDENCE",
                    is_fake=True, fake_reason="无联网搜索证据",
                    classification="概念蹭热",
                ))
                continue

            # 规则6: 搜索文本关键词分类
            if _CONFIRMED_KEYWORDS.search(ev_text):
                t["_rule_evidence_level"] = "PROBABLE_SUPPLIER"
            elif _CONCEPT_KEYWORDS.search(ev_text) and not _PROBABLE_KEYWORDS.search(ev_text):
                rule_classified.append(self._make_candidate(
                    company_name, t.get("symbol", ""), self._normalize_market(t.get("market", "OTHER"), t.get("symbol", "")),
                    node_name, main_dish, scores_result,
                    _safe_text(t.get("stress_test")), _safe_bool(t.get("stress_test_passed"), True),
                    evidence_links=filtered_links, evidence_level="CONCEPT_CLAIM_ONLY",
                    is_fake=True, fake_reason="搜索结果仅含概念性描述",
                    classification="概念蹭热",
                ))
                continue

            # 缓存检查
            cache_key = self._make_cache_key(company_name, node_name, ev_text)
            if cache_key in self._verification_cache:
                cached = self._verification_cache[cache_key]
                t["_cached_verification"] = cached
                t["_cache_hit"] = True
                logger.info("缓存命中: %s @ %s", company_name, node_name)
            else:
                t["_cache_hit"] = False

            need_llm.append(t)

        # ── Phase 4: Batch LLM 验证 ──
        llm_results: Dict[str, Dict[str, Any]] = {}  # company → verification result
        if need_llm and self._llm_available:
            # 分离缓存命中和需要 LLM 的
            cached_items = [t for t in need_llm if t.get("_cache_hit")]
            llm_items = [t for t in need_llm if not t.get("_cache_hit")]

            # 处理缓存命中
            for t in cached_items:
                cn = t.get("company_name", "")
                llm_results[cn] = t.get("_cached_verification", {})

            # Batch LLM 验证
            if llm_items:
                batches = [
                    llm_items[i:i + _BATCH_SIZE]
                    for i in range(0, len(llm_items), _BATCH_SIZE)
                ]
                self._progress(f"  Batch验证: {len(llm_items)} 家公司, {len(batches)} 个batch")

                def _verify_batch(batch: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
                    """验证一个 batch 的公司."""
                    batch_results: Dict[str, Dict[str, Any]] = {}
                    if len(batch) == 1:
                        # 单家公司直接用原有方法
                        t = batch[0]
                        cn = t.get("company_name", "")
                        ev_text = t.get("_search_text", "")
                        verified = self._verify_evidence_with_llm(cn, node_name, ev_text)
                        batch_results[cn] = verified
                        # 写缓存
                        cache_key = self._make_cache_key(cn, node_name, ev_text)
                        self._verification_cache[cache_key] = verified
                    else:
                        # 多家公司合并为一次 LLM 调用
                        batch_results = self._verify_evidence_batch_llm(batch, node_name)
                        # 写缓存
                        for t in batch:
                            cn = t.get("company_name", "")
                            ev_text = t.get("_search_text", "")
                            cache_key = self._make_cache_key(cn, node_name, ev_text)
                            if cn in batch_results:
                                self._verification_cache[cache_key] = batch_results[cn]
                    return batch_results

                with ThreadPoolExecutor(max_workers=_MAX_CONCURRENCY) as executor:
                    futures = {executor.submit(_verify_batch, b): b for b in batches}
                    for future in as_completed(futures):
                        try:
                            batch_result = future.result()
                            llm_results.update(batch_result)
                        except Exception as e:
                            logger.warning("Batch验证失败: %s", e)

        # ── Phase 5: 组装候选 ──
        candidates = list(rule_classified)

        for t in need_search:
            company_name = _safe_text(t.get("company_name"))
            if any(c.company_name == company_name for c in candidates):
                continue  # 已在 rule_classified 中

            symbol = _safe_text(t.get("symbol"))
            market = self._normalize_market(t.get("market", "OTHER"), symbol)
            classification = _safe_text(t.get("classification"), "相关") or "相关"
            stress_test = _safe_text(t.get("stress_test"))
            stress_passed = _safe_bool(t.get("stress_test_passed"), True)
            evidence_links = self._filter_evidence_links(t.get("_search_links", []))
            search_text = _safe_text(t.get("_search_text"))

            # 确定 evidence_level
            verified = llm_results.get(company_name, {})
            verified = verified if isinstance(verified, dict) else {}
            if verified:
                evidence_level = (
                    "CONFIRMED_SUPPLIER" if verified.get("evidence_sufficient")
                    else "PROBABLE_SUPPLIER" if search_text.strip()
                    else "INSUFFICIENT_EVIDENCE"
                )
            elif t.get("_rule_evidence_level"):
                evidence_level = t["_rule_evidence_level"]
            elif search_text.strip():
                evidence_level = "PROBABLE_SUPPLIER"
            else:
                evidence_level = "INSUFFICIENT_EVIDENCE"

            has_web_evidence = evidence_level in ("CONFIRMED_SUPPLIER", "PROBABLE_SUPPLIER")

            # 判断入池
            is_fake = not has_web_evidence
            fake_reason = ""
            in_pool = (
                not pricing_vetoed
                and total_score >= _CANDIDATE_MIN_SCORE
                and not is_fake
                and stress_passed
                and market in ("A_SHARE", "US")
                and classification == "紫苏叶"
            )

            if not in_pool and not is_fake:
                not_in_pool_reasons = []
                if pricing_vetoed:
                    not_in_pool_reasons.append("定价否决")
                if total_score < _CANDIDATE_MIN_SCORE:
                    not_in_pool_reasons.append(f"评分{total_score}<{_CANDIDATE_MIN_SCORE}")
                if not stress_passed:
                    not_in_pool_reasons.append("压力测试未通过")
                if classification != "紫苏叶":
                    not_in_pool_reasons.append(f"分类为{classification}（非紫苏叶）")
                fake_reason = "；".join(not_in_pool_reasons) if not_in_pool_reasons else "综合条件不满足"
            elif is_fake:
                fake_reason = "无联网搜索证据"

            candidates.append(self._make_candidate(
                company_name, symbol, market, node_name, main_dish,
                scores_result, stress_test, stress_passed,
                evidence_links=evidence_links, evidence_level=evidence_level,
                is_fake=is_fake, fake_reason=fake_reason,
                classification=classification,
            ))

        return candidates

    def _make_candidate(
        self,
        company_name: str, symbol: str, market: str,
        node_name: str, main_dish: str,
        scores_result: Dict[str, Any],
        stress_test: str, stress_test_passed: bool,
        evidence_links: List[str], evidence_level: str,
        is_fake: bool, fake_reason: str,
        classification: str,
    ) -> PerillaLeafCandidate:
        """快速构造 PerillaLeafCandidate（避免重复代码）."""
        scores_result = scores_result if isinstance(scores_result, dict) else {}
        company_name = _safe_text(company_name)
        symbol = _safe_text(symbol)
        market = self._normalize_market(market, symbol)
        node_name = _safe_text(node_name)
        main_dish = _safe_text(main_dish)
        stress_test = _safe_text(stress_test)
        stress_test_passed = _safe_bool(stress_test_passed, True)
        evidence_level = _safe_text(evidence_level, "INSUFFICIENT_EVIDENCE")
        classification = _safe_text(classification, "相关")
        is_fake = _safe_bool(is_fake)
        fake_reason = _safe_text(fake_reason)
        total_score = _safe_int(scores_result.get("total_score"), 0)
        pricing_vetoed = _safe_bool(scores_result.get("pricing_vetoed"))
        score_map = scores_result.get("scores") if isinstance(scores_result.get("scores"), dict) else {}
        reasoning = scores_result.get("reasoning") if isinstance(scores_result.get("reasoning"), dict) else {}
        evidence_links = [value for value in evidence_links if isinstance(value, str)] if isinstance(evidence_links, (list, tuple)) else []
        market_warning = f"非核心覆盖市场({market})" if market not in ("A_SHARE", "US") else ""

        in_pool = (
            not pricing_vetoed
            and total_score >= _CANDIDATE_MIN_SCORE
            and not is_fake
            and stress_test_passed
            and market in ("A_SHARE", "US")
            and evidence_level in ("CONFIRMED_SUPPLIER", "PROBABLE_SUPPLIER")
            and classification == "紫苏叶"
        )

        return PerillaLeafCandidate(
            candidate_id=new_id("pl_"),
            company_name=company_name,
            symbol=symbol,
            market=market,
            market_warning=market_warning,
            perilla_node=node_name,
            main_dish=main_dish,
            six_question_scores=score_map,
            total_score=total_score,
            pricing_vetoed=pricing_vetoed,
            bottleneck_logic=_safe_text(scores_result.get("bottleneck_logic")),
            stress_test=stress_test,
            stress_test_passed=stress_test_passed,
            evidence_links=evidence_links,
            evidence_level=evidence_level,
            is_fake_perilla=is_fake,
            fake_reason=fake_reason,
            in_pool=in_pool,
            six_question_reasoning=reasoning,
            company_classification=classification,
        )

    def _make_cache_key(self, company_name: str, node_name: str, evidence_text: str) -> str:
        """生成缓存键.

        Args:
            company_name: 公司名称。
            node_name: 节点名称。
            evidence_text: 证据文本。

        Returns:
            缓存键字符串。
        """
        evidence_text = _safe_text(evidence_text)
        company_name = _safe_text(company_name)
        node_name = _safe_text(node_name)
        evidence_hash = hashlib.md5(evidence_text.encode()).hexdigest()[:12]
        raw = f"{company_name}|{node_name}|{evidence_hash}|{_PROMPT_VERSION}|{_safe_text(getattr(self, '_llm_model_name', ''))}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _verify_evidence_batch_llm(
        self,
        batch: List[Dict[str, Any]],
        node_name: str,
    ) -> Dict[str, Dict[str, Any]]:
        """Batch LLM 验证：多家公司合并为1次 LLM 调用.

        Args:
            batch: 同一节点下的公司列表（含 _search_text）。
            node_name: 节点名称。

        Returns:
            {company_name: verification_result} 字典。
        """
        company_sections = []
        safe_batch = [t for t in batch if isinstance(t, dict)] if isinstance(batch, (list, tuple)) else []
        node_name = _safe_text(node_name)
        for t in safe_batch:
            cn = _safe_text(t.get("company_name"))
            symbol = _safe_text(t.get("symbol"))
            ev_text = _safe_text(t.get("_search_text"))[:500]
            rule_level = _safe_text(t.get("_rule_evidence_level"), "UNKNOWN")
            company_sections.append(
                f"### {cn} ({symbol})\n规则预分类: {rule_level}\n搜索证据:\n{ev_text}"
            )

        prompt = f"""你是供应链分析专家，判断以下公司是否为"{node_name}"环节的真实供应商。

## 判断标准
- CONFIRMED: 搜索证据明确提到供货/认证/订单/量产关系
- PROBABLE: 有间接证据（布局/研发/送样），但未确认量产供货
- CONCEPT: 仅有概念性描述，无实锤供货证据
- UNRELATED: 与该环节无关

## 公司列表
{chr(10).join(company_sections)}

## 输出要求
严格输出 JSON：
{{
  "verifications": [
    {{
      "company_name": "公司名",
      "evidence_sufficient": true/false,
      "evidence_level": "CONFIRMED|PROBABLE|CONCEPT|UNRELATED",
      "is_perilla_like": true/false,
      "reason": "判断理由"
    }}
  ]
}}"""

        result = self._call_llm(prompt)
        parsed = self._extract_json(result) if result else None

        if not parsed or "verifications" not in parsed:
            # 降级：逐个验证
            logger.warning("Batch LLM 验证失败，降级到逐个验证")
            fallback_results = {}
            for t in safe_batch:
                cn = _safe_text(t.get("company_name"))
                ev_text = _safe_text(t.get("_search_text"))
                verified = self._verify_evidence_with_llm(cn, node_name, ev_text)
                fallback_results[cn] = verified
            return fallback_results

        results = {}
        for v in parsed.get("verifications", []) if isinstance(parsed.get("verifications"), list) else []:
            if not isinstance(v, dict):
                continue
            cn = _safe_text(v.get("company_name"))
            level = _safe_text(v.get("evidence_level"), "CONCEPT") or "CONCEPT"
            results[cn] = {
                "evidence_sufficient": level in ("CONFIRMED", "PROBABLE"),
                "evidence_level": level,
                "is_perilla_like": v.get("is_perilla_like", False),
                "reason": v.get("reason", ""),
            }

        return results

    # ══════════════════════════════════════════════════════
    # Step 4: 落地标的
    # ══════════════════════════════════════════════════════

    def step4_map_to_tickers(
        self,
        node: Dict[str, Any],
        main_dish: str,
        topic: str,
        scores_result: Dict[str, Any],
    ) -> List[PerillaLeafCandidate]:
        """Step 4: 将瓶颈环节映射到具体上市公司.

        使用 LLM 推理 + 联网搜索验证供货证据。
        铁律：没有联网搜索到的供货证据，不得进入输出表。

        Args:
            node: 供应链节点字典。
            main_dish: 主菜描述。
            topic: 主题。
            scores_result: Step 2 的评分结果。

        Returns:
            紫苏叶候选标的列表。
        """
        node = node if isinstance(node, dict) else {}
        scores_result = scores_result if isinstance(scores_result, dict) else {}
        node_name = _safe_text(node.get("name"))
        node_layer = _safe_text(node.get("layer"), "unknown") or "unknown"
        main_dish = _safe_text(main_dish)
        topic = _safe_text(topic)
        bottleneck_logic = _safe_text(scores_result.get("bottleneck_logic"))

        if not self._llm_available:
            return self._step4_fallback(node, main_dish, scores_result)

        # 先搜索该环节的供应商信息，辅助 LLM 映射
        supplier_search = self._search_ticker_evidence("", node_name, topic)
        supplier_search_text = supplier_search[0] if supplier_search else ""

        search_section = ""
        if supplier_search_text:
            search_section = f"""
## 联网搜索到的供应商信息
{supplier_search_text[:2000]}
"""

        prompt = f"""你是一位供应链瓶颈分析专家，正在执行"紫苏叶选股法"的标的映射。

## 任务
将以下供应链瓶颈环节映射到具体的上市公司（仅美股和A股）。

### 铁律
1. 仅覆盖美股与 A 股；港股、日股等其他市场标记并说明
2. 每个标的必须有供货证据（公告、客户认证、产能数据）
3. 永远不推荐"主菜/牛肉"型标的（终端巨头、明星整机）
4. A 股"蹭概念"极多，无实锤供货证据的标记为 ⚠️ 伪紫苏叶
5. 优先基于搜索证据映射标的，不要凭记忆编造不存在的供货关系

### 压力测试
对每个标的写一句话："如果这家公司明天停产，链条上谁会停摆？"
答不上来 → 不是紫苏叶。

### A 股代码格式
- 上海: 6XXXXX.SH（如 603296.SH）
- 深圳: 0XXXXX.SZ 或 3XXXXX.SZ（如 300308.SZ）
- 北交所: XXXXXX.BJ
{search_section}
## 输入
主菜：{main_dish}
瓶颈环节：{node_name}（{node_layer}层）
瓶颈逻辑：{bottleneck_logic}

## 输出要求
严格输出 JSON：
{{
  "tickers": [
    {{
      "company_name": "公司名（纯名称，不含代码）",
      "symbol": "证券代码（如 300308.SZ 或 NVDA）",
      "market": "A_SHARE|US|HK|OTHER",
      "supply_evidence": "供货证据描述（必须具体：给谁供什么、认证状态等）",
      "evidence_source": "证据来源（如'公司公告'、'客户认证'、'行业研报'）",
      "stress_test": "如果这家公司明天停产，链条上谁会停摆？",
      "stress_test_passed": true/false,
      "is_fake_perilla": false,
      "fake_reason": ""
    }}
  ]
}}

要求：
1. 每个环节映射 1-3 家最相关的公司
2. 优先映射搜索证据中有明确供货关系的公司
3. 不确定的公司标记 is_fake_perilla=true 并说明原因
4. 美股公司给 ticker（如 COHR），A 股给代码.市场（如 688498.SH）
5. 如果搜索证据中没有找到具体供应商，宁可少映射也不要编造"""

        result = self._call_llm(prompt)
        parsed = self._extract_json(result) if result else None

        tickers_data = []
        if parsed and "tickers" in parsed:
            tickers_data = parsed["tickers"]
        tickers_data = [item for item in tickers_data if isinstance(item, dict)] if isinstance(tickers_data, list) else []

        if not tickers_data:
            logger.warning("Step 4 LLM 标的映射失败 (%s)，降级", node_name)
            return self._step4_fallback(node, main_dish, scores_result, reason="LLM标的映射JSON解析失败")

        # 对每个标的联网搜索验证供货证据
        candidates = []
        for t in tickers_data:
            company_name = _safe_text(t.get("company_name"))
            symbol = _safe_text(t.get("symbol"))
            market = self._normalize_market(t.get("market"), symbol)

            # 联网搜索验证
            search_evidence, evidence_links = self._search_ticker_evidence(
                company_name, node_name, topic,
            )

            # 如果有搜索证据，让 LLM 判断证据是否充分
            has_web_evidence = bool(search_evidence.strip())
            if has_web_evidence and self._llm_available:
                verified = self._verify_evidence_with_llm(
                    company_name, node_name, search_evidence,
                )
            else:
                verified = {
                    "evidence_sufficient": False,
                    "evidence_summary": "无联网搜索证据" if not has_web_evidence else "需要人工验证",
                }

            # 市场过滤
            market_warning = ""
            if market not in ("A_SHARE", "US"):
                market_warnings = {
                    "HK": "港股市场，非紫苏叶覆盖范围",
                    "JP": "日股市场，非紫苏叶覆盖范围",
                    "TW": "台股市场，非紫苏叶覆盖范围",
                }
                market_warning = market_warnings.get(market, "非核心市场，不在紫苏叶覆盖范围")

            # 压力测试
            stress_test = _safe_text(t.get("stress_test"))
            stress_passed = _safe_bool(t.get("stress_test_passed"), bool(stress_test))

            # 是否伪紫苏叶
            is_fake = _safe_bool(t.get("is_fake_perilla"))
            fake_reason = _safe_text(t.get("fake_reason"))

            # 铁律：没有联网搜索到的供货证据，不得进入输出表
            if not has_web_evidence and not is_fake:
                is_fake = True
                fake_reason = "无联网搜索到的供货证据，按铁律不得进入输出表"

            # 证据等级
            if verified.get("evidence_sufficient"):
                evidence_level = "CONFIRMED_SUPPLIER"
            elif has_web_evidence:
                evidence_level = "PROBABLE_SUPPLIER"
            else:
                evidence_level = "INSUFFICIENT_EVIDENCE"

            # 判断是否进入候选池
            total_score = scores_result.get("total_score", 0)
            pricing_vetoed = scores_result.get("pricing_vetoed", False)
            in_pool = (
                not pricing_vetoed
                and total_score >= _CANDIDATE_MIN_SCORE
                and not is_fake
                and stress_passed
                and market in ("A_SHARE", "US")
                and has_web_evidence
            )

            # 为未入池的非伪紫苏叶公司级候选设置具体淘汰原因
            if not in_pool and not is_fake and not fake_reason:
                not_in_pool_reasons = []
                if pricing_vetoed:
                    not_in_pool_reasons.append("定价否决")
                if total_score < _CANDIDATE_MIN_SCORE:
                    not_in_pool_reasons.append(f"评分{total_score}<{_CANDIDATE_MIN_SCORE}")
                if not stress_passed:
                    not_in_pool_reasons.append("压力测试未通过")
                if market not in ("A_SHARE", "US"):
                    not_in_pool_reasons.append(f"非A股/美股市场({market})")
                if not has_web_evidence:
                    not_in_pool_reasons.append("无联网搜索证据")
                fake_reason = "；".join(not_in_pool_reasons) if not_in_pool_reasons else "综合条件不满足"

            cand = PerillaLeafCandidate(
                candidate_id=new_id("pl_"),
                company_name=company_name,
                symbol=symbol,
                market=market,
                market_warning=market_warning,
                perilla_node=node_name,
                main_dish=main_dish,
                six_question_scores=scores_result.get("scores", {}),
                total_score=total_score,
                pricing_vetoed=pricing_vetoed,
                bottleneck_logic=bottleneck_logic,
                stress_test=stress_test,
                stress_test_passed=stress_passed,
                evidence_links=evidence_links,
                evidence_level=evidence_level,
                is_fake_perilla=is_fake,
                fake_reason=fake_reason,
                in_pool=in_pool,
                six_question_reasoning=scores_result.get("reasoning", {}),
            )
            candidates.append(cand)

        logger.info(
            "Step 4 标的映射: %s → %d 家公司, 入池 %d",
            node_name, len(candidates), sum(1 for c in candidates if c.in_pool),
        )
        return candidates

    def _verify_evidence_with_llm(
        self, company_name: str, node_name: str, search_evidence: str,
    ) -> Dict[str, Any]:
        """用 LLM 判断搜索证据是否充分证明供货关系.

        Args:
            company_name: 公司名称。
            node_name: 节点名称。
            search_evidence: 搜索到的证据文本。

        Returns:
            包含 evidence_sufficient 和 evidence_summary 的字典。
        """
        prompt = f"""判断以下搜索证据是否充分证明 "{company_name}" 在 "{node_name}" 环节的供货关系。

搜索证据：
{search_evidence[:1500]}

判断标准：
- 充分：有明确的供货/供应商/认证/量产/出货等关键词，且与{node_name}环节直接相关
- 不充分：只有概念提及、板块归属、无具体供货关系描述

严格输出 JSON：
{{
  "evidence_sufficient": true/false,
  "evidence_summary": "证据摘要（1-2句话）"
}}"""

        result = self._call_llm(prompt)
        parsed = self._extract_json(result) if result else None
        if parsed and "evidence_sufficient" in parsed:
            return parsed
        return {"evidence_sufficient": False, "evidence_summary": "无法判断"}

    @staticmethod
    def _step4_fallback(
        node: Dict[str, Any], main_dish: str, scores_result: Dict[str, Any],
        reason: str = "LLM不可用",
    ) -> List[PerillaLeafCandidate]:
        """Step 4 降级：无法映射标的时返回标记候选.

        Args:
            node: 供应链节点字典。
            main_dish: 主菜描述。
            scores_result: 评分结果。
            reason: 降级原因。

        Returns:
            标记为伪紫苏叶的候选列表。
        """
        node = node if isinstance(node, dict) else {}
        scores_result = scores_result if isinstance(scores_result, dict) else {}
        node_name = _safe_text(node.get("name"))
        main_dish = _safe_text(main_dish)
        reason = _safe_text(reason, "LLM不可用")
        total_score = _safe_int(scores_result.get("total_score"), 0)
        pricing_vetoed = _safe_bool(scores_result.get("pricing_vetoed"))

        return [PerillaLeafCandidate(
            candidate_id=new_id("pl_"),
            company_name=f"待验证的{node_name}供应商",
            symbol="",
            market="UNKNOWN",
            perilla_node=node_name,
            main_dish=main_dish,
            six_question_scores=scores_result.get("scores", {}),
            total_score=total_score,
            pricing_vetoed=pricing_vetoed,
            bottleneck_logic=scores_result.get("bottleneck_logic", ""),
            is_fake_perilla=True,
            fake_reason=f"{reason}，无法映射具体标的，需要人工研究",
            in_pool=False,
        )]

    # ══════════════════════════════════════════════════════
    # Step 5: 蓝军反检验
    # ══════════════════════════════════════════════════════

    def step5_red_team(
        self, candidates: List[PerillaLeafCandidate],
    ) -> List[PerillaLeafCandidate]:
        """Step 5: 蓝军反检验（规则预筛 + batch LLM + 并发）.

        规则预筛：CONCEPT_CLAIM_ONLY / RUMOR_ONLY / NON_CORE_MARKET /
        MAIN_ANCHOR / INSUFFICIENT_EVIDENCE → 不做深蓝军，只加提示。
        蓝军 LLM 只处理：紫苏叶 / PROBABLE_SUPPLIER / EARLY_SIGNAL + 高分。

        Args:
            candidates: 候选标的列表。

        Returns:
            标注风险后的候选列表。
        """
        if not isinstance(candidates, (list, tuple)) or not candidates:
            return []
        candidates = [c for c in candidates if isinstance(c, PerillaLeafCandidate)]
        if not candidates:
            return []

        # ── Phase 1: 规则预筛 ──
        need_llm: List[PerillaLeafCandidate] = []
        for c in candidates:
            # 不入池的不做深蓝军
            if not c.in_pool:
                # 规则蓝军：加简单提示
                c.red_team_risks = []
                c.risk_level = "low"
                continue

            # 规则预筛：这些分类不需要深蓝军
            if c.company_classification in ("概念蹭热", "相关"):
                c.red_team_risks = [f"分类为{c.company_classification}，非紫苏叶核心候选"]
                c.risk_level = "medium"
                continue

            if c.evidence_level in ("INSUFFICIENT_EVIDENCE", "CONCEPT_CLAIM_ONLY"):
                c.red_team_risks = ["证据不充分，需进一步验证"]
                c.risk_level = "high"
                continue

            if c.market not in ("A_SHARE", "US"):
                c.red_team_risks = [f"非核心覆盖市场({c.market})"]
                c.risk_level = "medium"
                continue

            # 需要深蓝军：紫苏叶 / PROBABLE_SUPPLIER / EARLY_SIGNAL + 高分
            need_llm.append(c)

        if not need_llm:
            self._progress(f"  蓝军: 全部规则预筛完成，0家需LLM深审")
            return candidates

        # ── Phase 2: Batch LLM 蓝军 ──
        if not self._llm_available:
            return self._step5_fallback(candidates)

        self._progress(f"  蓝军: {len(need_llm)} 家需LLM深审")

        # 分批
        batches = [
            need_llm[i:i + _BATCH_SIZE]
            for i in range(0, len(need_llm), _BATCH_SIZE)
        ]

        def _review_batch(batch: List[PerillaLeafCandidate]) -> Dict[str, Dict[str, Any]]:
            """对一个 batch 做蓝军审查."""
            batch_summary = []
            for c in batch:
                batch_summary.append({
                    "company_name": c.company_name,
                    "symbol": c.symbol,
                    "market": c.market,
                    "perilla_node": c.perilla_node,
                    "bottleneck_logic": c.bottleneck_logic,
                    "total_score": c.total_score,
                    "classification": c.company_classification,
                })

            prompt = f"""你是一位蓝军分析师，正在对"紫苏叶选股法"的候选标的进行反检验。

## 任务
对每个候选标的进行攻击：

1. **替代风险**：是否存在被下游内制化或技术路线替代的风险（如铜退光进、路线切换）？
2. **客户集中度**：单一客户 >30% 营收？
3. **治理风险**（A 股）：解禁、减持、商誉等公司治理雷。
4. **路线变更风险**：技术路线是否可能发生根本性变化？

经不起攻击的标注风险等级，不直接删除，让用户看到完整推理。

## 候选标的
{json.dumps(batch_summary, ensure_ascii=False, indent=2)}

## 输出要求
严格输出 JSON：
{{
  "reviews": [
    {{
      "company_name": "公司名",
      "substitution_risk": "替代风险描述",
      "customer_concentration_risk": "客户集中度风险描述",
      "governance_risk": "治理风险描述（非A 股填'不适用'）",
      "route_change_risk": "路线变更风险描述",
      "overall_risk_level": "low|medium|high",
      "should_downgrade": false,
      "downgrade_reason": ""
    }}
  ]
}}"""

            result = self._call_llm(prompt)
            parsed = self._extract_json(result) if result else None

            if not parsed:
                return {}

            reviews = parsed.get("reviews", parsed.get("assessments", []))
            return {
                _safe_text(r.get("company_name")): r
                for r in reviews if isinstance(r, dict) and _safe_text(r.get("company_name"))
            } if isinstance(reviews, list) else {}

        # 并发执行 batch
        all_reviews: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENCY) as executor:
            futures = {executor.submit(_review_batch, b): b for b in batches}
            for future in as_completed(futures):
                try:
                    batch_reviews = future.result()
                    all_reviews.update(batch_reviews)
                except Exception as e:
                    logger.warning("蓝军batch失败: %s", e)

        # 合并结果
        for c in need_llm:
            review = all_reviews.get(c.company_name)
            if not review:
                c.red_team_risks = ["蓝军审查未完成"]
                c.risk_level = "medium"
                continue

            risks = []
            review = review if isinstance(review, dict) else {}
            sub_risk = _safe_text(review.get("substitution_risk"))
            if sub_risk and "暂无" not in sub_risk and "无明显" not in sub_risk:
                risks.append(f"替代风险: {sub_risk}")
            conc_risk = _safe_text(review.get("customer_concentration_risk"))
            if conc_risk and "不适用" not in conc_risk and "暂无" not in conc_risk:
                risks.append(f"客户集中度: {conc_risk}")
            gov_risk = _safe_text(review.get("governance_risk"))
            if gov_risk and "不适用" not in gov_risk and "暂无" not in gov_risk:
                risks.append(f"治理风险: {gov_risk}")
            route_risk = _safe_text(review.get("route_change_risk"))
            if route_risk and "暂无" not in route_risk and "无明显" not in route_risk:
                risks.append(f"路线变更: {route_risk}")

            c.red_team_risks = risks
            risk_level = _safe_text(review.get("overall_risk_level"), "medium")
            c.risk_level = risk_level if risk_level in ("low", "medium", "high") else "medium"

            # 降级处理
            if _safe_bool(review.get("should_downgrade")) and c.in_pool:
                c.in_pool = False
                c.fake_reason = f"蓝军降级: {_safe_text(review.get('downgrade_reason'), '风险过高')}"

        return candidates

    @staticmethod
    def _step5_fallback(candidates: List[PerillaLeafCandidate]) -> List[PerillaLeafCandidate]:
        """Step 5 降级：简单规则蓝军检验."""
        if not isinstance(candidates, list):
            return []
        candidates = [c for c in candidates if isinstance(c, PerillaLeafCandidate)]
        for c in candidates:
            if not c.in_pool:
                continue
            risks = []
            # 简单规则：A 股标记一般治理风险提示
            if c.market == "A_SHARE":
                risks.append("治理风险: A股标的需关注解禁/减持/商誉等风险")
            c.red_team_risks = risks
            c.risk_level = "medium" if risks else "low"
        return candidates

    # ══════════════════════════════════════════════════════
    # Step 6: 输出
    # ══════════════════════════════════════════════════════

    def step6_generate_report(
        self,
        topic: str,
        main_dish: str,
        chain_nodes: List[Dict[str, Any]],
        candidates: List[PerillaLeafCandidate],
    ) -> PerillaLeafReport:
        """Step 6: 生成紫苏叶输出报告.

        Args:
            topic: 输入主题。
            main_dish: 主菜描述。
            chain_nodes: 供应链节点列表。
            candidates: 候选标的列表。

        Returns:
            紫苏叶报告。
        """
        topic = _safe_text(topic)
        main_dish = _safe_text(main_dish)
        safe_nodes = [n for n in chain_nodes if isinstance(n, dict)] if isinstance(chain_nodes, (list, tuple)) else []
        safe_candidates = [c for c in candidates if isinstance(c, PerillaLeafCandidate)] if isinstance(candidates, (list, tuple)) else []
        in_pool = [c for c in safe_candidates if c.in_pool]
        eliminated = [c for c in safe_candidates if not c.in_pool]

        # 最有信心的 1-3 个（按总分降序，风险等级升序）
        risk_order = {"low": 0, "medium": 1, "high": 2}
        in_pool_sorted = sorted(
            in_pool,
            key=lambda c: (-_safe_int(c.total_score), risk_order.get(_safe_text(c.risk_level), 1)),
        )
        top_picks = in_pool_sorted[:3]

        # 产业链摘要
        chain_summary = self._summarize_chain_nodes(safe_nodes)

        return PerillaLeafReport(
            run_id=new_id("plr_"),
            topic=topic,
            main_dish=main_dish,
            chain_summary=chain_summary,
            candidates=safe_candidates,
            top_picks=top_picks,
            eliminated=eliminated,
            disclaimer=self._disclaimer(),
            generated_at=now_iso(),
            llm_used=self._llm_available,
            web_search_used=self._search_available,
        )

    @staticmethod
    def _summarize_chain_nodes(chain_nodes: List[Dict[str, Any]]) -> str:
        """从节点列表生成产业链摘要."""
        if not isinstance(chain_nodes, (list, tuple)):
            return "产业链为空"
        chain_nodes = [node for node in chain_nodes if isinstance(node, dict)]
        if not chain_nodes:
            return "产业链为空"

        layer_order = [
            "terminal", "module", "component", "material",
            "equipment", "testing", "consumable", "service", "software",
        ]
        layer_names = {
            "terminal": "终端", "module": "模组", "component": "器件",
            "material": "材料", "equipment": "设备", "testing": "测试",
            "consumable": "耗材", "service": "服务", "software": "软件",
            "unknown": "其他",
        }

        parts: List[str] = []
        for layer in layer_order:
            nodes = [n for n in chain_nodes if _safe_text(n.get("layer")) == layer]
            if nodes:
                names = "、".join(_safe_text(n.get("name")) for n in nodes[:5] if _safe_text(n.get("name")))
                parts.append(f"{layer_names.get(layer, layer)}: {names}")

        return " → ".join(parts)

    @staticmethod
    def _disclaimer() -> str:
        """免责声明."""
        return (
            "免责声明：本报告由紫苏叶选股法（Perilla Leaf Screener）自动生成，"
            "仅供研究方法论参考，不构成任何投资建议。"
            "紫苏叶选股法是一种供应链瓶颈分析框架，"
            "不推荐买入、卖出、持有任何证券。"
            "供应链格局随时变化，过往的瓶颈地位不代表未来。"
            "任何标的没有当日联网搜索到的供货证据，不得作为投资依据。"
            "投资有风险，决策需自行判断。"
        )

    # ══════════════════════════════════════════════════════
    # 完整工作流
    # ══════════════════════════════════════════════════════

    def run(
        self,
        topic: str,
        preset_signals_path: Optional[str] = None,
        mode: str = "fast",
    ) -> PerillaLeafReport:
        """执行紫苏叶六步工作流.

        DAG 流程：概念发现 → 产业链构建 → 瓶颈节点列表 → 节点并行评估
        → 全局合并 → 蓝军 batch review → 报告

        Args:
            topic: 输入主题（如"光互连"、"AIPC"、"人形机器人"）。
            preset_signals_path: 预置信号 JSON 文件路径。
            mode: "fast"（60-180s，初步结果）或 "deep"（5-10min，完整验证）。

        Returns:
            紫苏叶报告。
        """
        topic = _safe_text(topic).strip()
        if not topic:
            return self.step6_generate_report("", "", [], [])
        mode = _safe_text(mode).lower()
        is_deep = mode == "deep"
        mode_label = "Deep" if is_deep else "Fast"
        self._progress(f"===== 紫苏叶选股法[{mode_label}] 启动: {topic} =====")
        self._progress(f"LLM: {self._llm_available}, 联网搜索: {self._search_available}")

        # 加载预置信号
        preset_signals = self._load_preset_signals(preset_signals_path, topic)
        if preset_signals:
            self._progress(f"加载预置信号: {len(preset_signals)} 条")

        # Step 0: 定主菜
        self._progress("[Step 0/6] 定主菜...")
        main_dish = self.step0_define_main_dish(topic)
        self._progress(f"[Step 0/6] 主菜: {main_dish}")

        # Step 1: 拆链下钻
        self._progress("[Step 1/6] 拆链下钻（LLM + 联网搜索）...")
        chain_nodes = self.step1_drill_down_chain(main_dish, topic)
        chain_nodes = [n for n in chain_nodes if isinstance(n, dict)]
        perilla_nodes = [n for n in chain_nodes if _safe_bool(n.get("is_perilla_candidate"), True)]
        self._progress(f"[Step 1/6] 拆链完成: {len(chain_nodes)} 个节点, {len(perilla_nodes)} 个紫苏叶候选")

        # Step 2-4: 节点并行评估
        eval_nodes = [n for n in perilla_nodes if _safe_text(n.get("layer")) not in _MAIN_DISH_LAYERS]
        total_perilla = len(eval_nodes)
        self._progress(f"[Step 2-4/6] 并行评估 {total_perilla} 个节点 (concurrency={min(_MAX_CONCURRENCY, total_perilla)})...")

        all_candidates: List[PerillaLeafCandidate] = []
        node_results: Dict[str, Tuple[Dict[str, Any], List[PerillaLeafCandidate]]] = {}

        def _eval_node(idx_node: Tuple[int, Dict[str, Any]]) -> Tuple[str, Dict[str, Any], List[PerillaLeafCandidate]]:
            """评估单个节点（线程安全）."""
            idx, node = idx_node
            node_name = _safe_text(node.get("name"))
            self._progress(f"  [节点 {idx+1}/{total_perilla}] 开始: {node_name}")
            scores_result, node_candidates = self.evaluate_node_full(
                node, main_dish, topic, preset_signals=preset_signals,
            )
            scores_result = scores_result if isinstance(scores_result, dict) else {}
            total_score = _safe_int(scores_result.get("total_score"), 0)
            in_pool = _safe_bool(scores_result.get("in_pool"))
            self._progress(f"  [节点 {idx+1}/{total_perilla}] 完成: {node_name} → {total_score}/12, {len(node_candidates)} 家公司")
            return node_name, scores_result, node_candidates

        # 并行执行节点评估
        with ThreadPoolExecutor(max_workers=max(1, min(_MAX_CONCURRENCY, total_perilla))) as executor:
            futures = {
                executor.submit(_eval_node, (idx, node)): node
                for idx, node in enumerate(eval_nodes)
            }
            for future in as_completed(futures):
                try:
                    node_name, scores_result, node_candidates = future.result()
                    node_results[node_name] = (scores_result, node_candidates)
                except Exception as e:
                    logger.warning("节点评估失败: %s", e)

        # 按原始顺序合并结果
        for node in eval_nodes:
            node_name = _safe_text(node.get("name"))
            if node_name not in node_results:
                continue

            scores_result, node_candidates = node_results[node_name]
            scores_result = scores_result if isinstance(scores_result, dict) else {}
            node_candidates = [c for c in node_candidates if isinstance(c, PerillaLeafCandidate)] if isinstance(node_candidates, (list, tuple)) else []
            total_score = _safe_int(scores_result.get("total_score"), 0)
            pricing_vetoed = _safe_bool(scores_result.get("pricing_vetoed"))
            in_pool = _safe_bool(scores_result.get("in_pool"))

            # 不入池的节点也记录
            if not in_pool and not node_candidates:
                all_candidates.append(PerillaLeafCandidate(
                    candidate_id=new_id("pl_"),
                    company_name="",
                    symbol="",
                    market="UNKNOWN",
                    perilla_node=node_name,
                    main_dish=main_dish,
                    six_question_scores=scores_result.get("scores", {}),
                    total_score=total_score,
                    pricing_vetoed=pricing_vetoed,
                    bottleneck_logic=scores_result.get("bottleneck_logic", ""),
                    six_question_reasoning=scores_result.get("reasoning", {}),
                    in_pool=False,
                    is_fake_perilla=False,
                    fake_reason="定价否决" if pricing_vetoed else f"评分{total_score}<{_CANDIDATE_MIN_SCORE}",
                ))
                continue

            all_candidates.extend(node_candidates)

            # 节点入池但所有标的都未入池 → 补充节点级淘汰记录
            in_pool_tickers = [c for c in node_candidates if c.in_pool]
            if not in_pool_tickers and node_candidates and in_pool:
                reasons = []
                fakes = [c for c in node_candidates if c.is_fake_perilla]
                non_core = [c for c in node_candidates if c.market not in ("A_SHARE", "US")]
                stress_failed = [c for c in node_candidates if not c.stress_test_passed and not c.is_fake_perilla]
                non_perilla = [c for c in node_candidates if c.company_classification != "紫苏叶" and not c.is_fake_perilla]
                if fakes:
                    reasons.append(f"{len(fakes)}家标的无供货证据（伪紫苏叶）")
                if non_core:
                    markets = ", ".join(set(c.market for c in non_core))
                    reasons.append(f"{len(non_core)}家标的为非A股/美股市场({markets})")
                if stress_failed:
                    reasons.append(f"{len(stress_failed)}家标的压力测试未通过")
                if non_perilla:
                    classifications = ", ".join(set(c.company_classification for c in non_perilla))
                    reasons.append(f"{len(non_perilla)}家分类为{classifications}（非紫苏叶）")
                if not reasons:
                    reasons.append("所有映射标的均未通过入池条件")
                all_candidates.append(PerillaLeafCandidate(
                    candidate_id=new_id("pl_"),
                    company_name="",
                    symbol="",
                    market="UNKNOWN",
                    perilla_node=node_name,
                    main_dish=main_dish,
                    six_question_scores=scores_result.get("scores", {}),
                    total_score=total_score,
                    pricing_vetoed=pricing_vetoed,
                    bottleneck_logic=scores_result.get("bottleneck_logic", ""),
                    six_question_reasoning=scores_result.get("reasoning", {}),
                    in_pool=False,
                    is_fake_perilla=False,
                    fake_reason="；".join(reasons),
                ))

        # Fast 模式：规则蓝军 + 立即输出
        if not is_deep:
            self._progress("[Step 5/6] Fast模式：规则蓝军预筛...")
            all_candidates = self._step5_rule_only(all_candidates)
            in_pool_count = sum(1 for c in all_candidates if c.in_pool)
            self._progress(f"[Step 5/6] 规则蓝军完成: {in_pool_count} 入池")

            self._progress("[Step 6/6] 生成初步报告...")
            report = self.step6_generate_report(topic, main_dish, chain_nodes, all_candidates)
            report.report_status = "PRELIMINARY_PERILLA_DISCOVERY"
            self._progress(f"[Step 6/6] Fast报告完成: {len(report.top_picks)} Top picks")
            return report

        # Deep 模式：完整蓝军 LLM
        self._progress("[Step 5/6] Deep模式：蓝军反检验（规则预筛 + batch LLM）...")
        all_candidates = self.step5_red_team(all_candidates)

        in_pool_count = sum(1 for c in all_candidates if c.in_pool)
        self._progress(f"[Step 5/6] 蓝军检验完成: {in_pool_count} 入池, {len(all_candidates) - in_pool_count} 淘汰")

        # Step 6: 输出
        self._progress("[Step 6/6] 生成完整报告...")
        report = self.step6_generate_report(
            topic, main_dish, chain_nodes, all_candidates,
        )
        report.report_status = "DEEP_REVIEW_READY"
        self._progress(f"[Step 6/6] Deep报告完成: {len(report.top_picks)} Top picks, {len(report.eliminated)} 淘汰")

        return report

    @staticmethod
    def _step5_rule_only(candidates: List[PerillaLeafCandidate]) -> List[PerillaLeafCandidate]:
        """Fast 模式蓝军：仅规则审查，不调 LLM.

        Args:
            candidates: 候选标的列表。

        Returns:
            标注风险后的候选列表。
        """
        for c in candidates:
            if not c.in_pool:
                c.red_team_risks = []
                c.risk_level = "low"
                continue

            risks = []
            if c.market == "A_SHARE":
                risks.append("治理风险: A股标的需关注解禁/减持/商誉等风险")
            if c.company_classification != "紫苏叶":
                risks.append(f"分类为{c.company_classification}，非紫苏叶核心候选")
            if c.evidence_level in ("INSUFFICIENT_EVIDENCE", "CONCEPT_CLAIM_ONLY"):
                risks.append("证据不充分，需进一步验证")

            c.red_team_risks = risks
            c.risk_level = "high" if risks else "low"
        return candidates

    @staticmethod
    def _load_preset_signals(
        path: Optional[str], topic: str,
    ) -> List[Dict[str, Any]]:
        """加载预置信号 JSON 文件.

        Args:
            path: JSON 文件路径。
            topic: 主题（用于过滤信号）。

        Returns:
            信号列表。
        """
        if not path:
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            signals = data.get("signals", []) if isinstance(data, dict) else []
            signals = [s for s in signals if isinstance(s, dict)] if isinstance(signals, list) else []
            topic = _safe_text(topic)
            # 过滤匹配主题的信号
            if topic:
                signals = [s for s in signals if topic in s.get("concept_name", "")]
            return signals
        except Exception as e:
            logger.warning("加载预置信号失败: %s", e)
            return []

    def run_from_pipeline(
        self,
        concept: ConceptSignal,
        anchors: List[DemandAnchor],
        chain: DynamicSupplyChain,
        bottlenecks: List,
        company_candidates: List[CompanyCandidate],
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> PerillaLeafReport:
        """从管道模式运行，利用已有数据 + LLM 增强分析.

        将管道的供应链数据转为节点字典，然后调用 LLM 增强的六步工作流。

        Args:
            concept: 概念信号。
            anchors: 需求锚点。
            chain: 动态供应链。
            bottlenecks: 瓶颈假设列表。
            company_candidates: 公司候选列表。
            source_docs: 来源文档。
            early_signals: 早期信号。

        Returns:
            紫苏叶报告。
        """
        topic = _safe_text(getattr(concept, "name", ""))

        # 将管道的供应链节点转为字典格式
        chain_nodes = []
        raw_nodes = getattr(chain, "nodes", ())
        for n in raw_nodes if isinstance(raw_nodes, (list, tuple)) else []:
            if not isinstance(n, SupplyChainNode):
                continue
            # 从 early_signals 和 source_docs 收集节点相关证据
            node_evidence = self._collect_node_evidence_from_pipeline(
                n, source_docs, early_signals,
            )
            chain_nodes.append({
                "name": _safe_text(n.name),
                "layer": _safe_text(n.layer, "unknown") or "unknown",
                "description": _safe_text(n.description) or node_evidence[:200],
                "is_perilla_candidate": _safe_text(n.layer) in _PERILLA_LAYERS,
                "perilla_reason": _safe_text(n.hypothesis_reason),
            })

        # 如果管道的链太浅（<5 个节点），用 LLM 补充
        if len(chain_nodes) < 5 and self._llm_available:
            main_dish = self.step0_define_main_dish(topic)
            llm_nodes = self.step1_drill_down_chain(main_dish, topic)
            # 合并：管道节点优先，LLM 补充
            existing_names = {_safe_text(n.get("name")) for n in chain_nodes}
            for ln in llm_nodes if isinstance(llm_nodes, list) else []:
                if not isinstance(ln, dict):
                    continue
                ln_name = _safe_text(ln.get("name"))
                if ln_name and ln_name not in existing_names:
                    chain_nodes.append(ln)
                    existing_names.add(ln_name)

        # 执行六步工作流
        return self.run_with_chain_nodes(topic, chain_nodes)

    def run_with_chain_nodes(
        self,
        topic: str,
        chain_nodes: List[Dict[str, Any]],
    ) -> PerillaLeafReport:
        """给定供应链节点，执行六步工作流.

        Args:
            topic: 主题。
            chain_nodes: 供应链节点列表。

        Returns:
            紫苏叶报告。
        """
        # Step 0
        topic = _safe_text(topic).strip()
        chain_nodes = [n for n in chain_nodes if isinstance(n, dict)] if isinstance(chain_nodes, (list, tuple)) else []
        main_dish = self.step0_define_main_dish(topic)
        logger.info("Step 0 主菜: %s", main_dish)

        perilla_nodes = [n for n in chain_nodes if _safe_bool(n.get("is_perilla_candidate"), True)]
        logger.info("链节点: %d, 紫苏叶候选: %d", len(chain_nodes), len(perilla_nodes))

        # Step 2+3+4
        all_candidates: List[PerillaLeafCandidate] = []

        for node in perilla_nodes:
            node_name = _safe_text(node.get("name"))
            node_layer = _safe_text(node.get("layer"), "unknown") or "unknown"

            if node_layer in _MAIN_DISH_LAYERS:
                continue

            scores_result = self.step2_six_questions(node, main_dish, topic)
            scores_result = scores_result if isinstance(scores_result, dict) else {}
            total_score = _safe_int(scores_result.get("total_score"), 0)
            pricing_vetoed = _safe_bool(scores_result.get("pricing_vetoed"))
            in_pool = _safe_bool(scores_result.get("in_pool"))

            if not in_pool:
                all_candidates.append(PerillaLeafCandidate(
                    candidate_id=new_id("pl_"),
                    company_name="",
                    symbol="",
                    market="UNKNOWN",
                    perilla_node=node_name,
                    main_dish=main_dish,
                    six_question_scores=scores_result.get("scores", {}),
                    total_score=total_score,
                    pricing_vetoed=pricing_vetoed,
                    bottleneck_logic=scores_result.get("bottleneck_logic", ""),
                    six_question_reasoning=scores_result.get("reasoning", {}),
                    in_pool=False,
                    is_fake_perilla=False,
                    fake_reason="定价否决" if pricing_vetoed else f"评分{total_score}<{_CANDIDATE_MIN_SCORE}",
                ))
                continue

            node_candidates = self.step4_map_to_tickers(
                node, main_dish, topic, scores_result,
            )
            node_candidates = [c for c in node_candidates if isinstance(c, PerillaLeafCandidate)] if isinstance(node_candidates, (list, tuple)) else []
            all_candidates.extend(node_candidates)

            # 如果节点入池但所有映射标的都未入池，补充一条节点级淘汰记录
            in_pool_tickers = [c for c in node_candidates if c.in_pool]
            if not in_pool_tickers and node_candidates:
                reasons = []
                fakes = [c for c in node_candidates if c.is_fake_perilla]
                non_core = [c for c in node_candidates if c.market not in ("A_SHARE", "US")]
                stress_failed = [c for c in node_candidates if not c.stress_test_passed and not c.is_fake_perilla]
                if fakes:
                    reasons.append(f"{len(fakes)}家标的无供货证据（伪紫苏叶）")
                if non_core:
                    markets = ", ".join(set(c.market for c in non_core))
                    reasons.append(f"{len(non_core)}家标的为非A股/美股市场({markets})")
                if stress_failed:
                    reasons.append(f"{len(stress_failed)}家标的压力测试未通过")
                if not reasons:
                    reasons.append("所有映射标的均未通过入池条件")
                all_candidates.append(PerillaLeafCandidate(
                    candidate_id=new_id("pl_"),
                    company_name="",
                    symbol="",
                    market="UNKNOWN",
                    perilla_node=node_name,
                    main_dish=main_dish,
                    six_question_scores=scores_result.get("scores", {}),
                    total_score=total_score,
                    pricing_vetoed=pricing_vetoed,
                    bottleneck_logic=scores_result.get("bottleneck_logic", ""),
                    six_question_reasoning=scores_result.get("reasoning", {}),
                    in_pool=False,
                    is_fake_perilla=False,
                    fake_reason="；".join(reasons),
                ))

        # Step 5
        all_candidates = self.step5_red_team(all_candidates)

        # Step 6
        return self.step6_generate_report(topic, main_dish, chain_nodes, all_candidates)

    @staticmethod
    def _collect_node_evidence_from_pipeline(
        node: SupplyChainNode,
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> str:
        """从管道数据中收集节点相关证据文本."""
        if not isinstance(node, SupplyChainNode):
            return ""
        texts: List[str] = []
        if node.description:
            texts.append(_safe_text(node.description))
        if node.hypothesis_reason:
            texts.append(_safe_text(node.hypothesis_reason))

        for sig in early_signals if isinstance(early_signals, (list, tuple)) else []:
            if not isinstance(sig, EarlySignal):
                continue
            claim = _safe_text(sig.claim)
            evidence = _safe_text(sig.evidence_text)
            if node.name in claim or node.name in evidence:
                texts.append(claim[:200])
                if evidence:
                    texts.append(evidence[:200])

        for doc in source_docs if isinstance(source_docs, (list, tuple)) else []:
            if not isinstance(doc, SourceDocument):
                continue
            content = _safe_text(doc.content_text)
            if node.name in content:
                texts.append(content[:300])

        return " ".join(texts)


# ══════════════════════════════════════════════════════════
# 报告渲染
# ══════════════════════════════════════════════════════════

def render_perilla_leaf_report_md(report: PerillaLeafReport) -> str:
    """将紫苏叶报告渲染为 Markdown.

    Args:
        report: 紫苏叶报告。

    Returns:
        Markdown 字符串。
    """
    if not isinstance(report, PerillaLeafReport):
        return ""

    def valid_candidates(value: Any) -> List[PerillaLeafCandidate]:
        if not isinstance(value, (list, tuple)):
            return []
        return [item for item in value if isinstance(item, PerillaLeafCandidate)]

    def text_list(value: Any) -> List[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        return [item for item in (_safe_text(item) for item in value) if item]

    candidates = valid_candidates(getattr(report, "candidates", []))
    top_picks = valid_candidates(getattr(report, "top_picks", []))
    eliminated = valid_candidates(getattr(report, "eliminated", []))
    topic = _safe_text(getattr(report, "topic", ""))
    report_status = _safe_text(getattr(report, "report_status", "COMPLETED"))
    main_dish = _safe_text(getattr(report, "main_dish", ""))
    generated_at = _safe_text(getattr(report, "generated_at", ""))
    chain_summary = _safe_text(getattr(report, "chain_summary", ""))
    disclaimer = _safe_text(getattr(report, "disclaimer", ""))

    lines: List[str] = []

    lines.append(f"# 紫苏叶选股法报告: {topic}")
    lines.append("")

    # 报告状态提示
    status_labels = {
        "PRELIMINARY_PERILLA_DISCOVERY": "⚡ Fast模式 — 初步结果，蓝军审查为规则预筛",
        "DEEP_REVIEW_READY": "🔍 Deep模式 — 完整验证，含LLM蓝军审查",
        "COMPLETED": "✅ 完成",
    }
    status_text = status_labels.get(report_status, report_status)
    lines.append(f"**状态**: {status_text}")
    lines.append("")

    lines.append(f"**主菜**: {main_dish}")
    lines.append(f"**生成时间**: {generated_at}")
    lines.append(f"**产业链**: {chain_summary}")
    lines.append(f"**LLM 驱动**: {'是' if _safe_bool(getattr(report, 'llm_used', False)) else '否（规则降级模式）'}")
    lines.append(f"**联网搜索**: {'是' if _safe_bool(getattr(report, 'web_search_used', False)) else '否（无搜索验证）'}")
    lines.append("")

    # ── 五级分类分组 ──
    classification_order = ["紫苏叶", "主链锚点", "相关", "早期信号", "概念蹭热"]
    classification_emoji = {
        "紫苏叶": "🍃",
        "主链锚点": "⚓",
        "相关": "🔗",
        "早期信号": "🌱",
        "概念蹭热": "⚠️",
    }

    for cls in classification_order:
        cls_candidates = [
            c for c in candidates
            if c.company_classification == cls and c.company_name
        ]
        if not cls_candidates:
            continue

        emoji = classification_emoji.get(cls, "")
        lines.append(f"## {emoji} {cls}")
        lines.append("")

        if cls == "紫苏叶":
            # 紫苏叶：完整表格
            lines.append("| 标的(代码/市场) | 紫苏叶环节 | 主菜 | 评分 | 一句话瓶颈逻辑 | 主要风险 | 证据链接 |")
            lines.append("|---|---|---|---|---|---|---|")
            for c in cls_candidates:
                clean_name = _safe_text(c.company_name).split("(")[0].split("（")[0].strip()
                symbol = _safe_text(c.symbol)
                if symbol:
                    symbol_market = f"{clean_name}({symbol})"
                else:
                    symbol_market = clean_name

                risks = text_list(c.red_team_risks)
                evidence_links = text_list(c.evidence_links)
                risk_text = "；".join(risks) if risks else "暂无"
                evidence_level = _safe_text(c.evidence_level)
                if evidence_links:
                    evidence_text = "；".join(evidence_links[:3])
                elif evidence_level == "CONFIRMED_SUPPLIER":
                    evidence_text = "LLM验证通过"
                elif evidence_level == "PROBABLE_SUPPLIER":
                    evidence_text = "搜索证据待进一步验证"
                else:
                    evidence_text = "暂无直接证据链接"

                lines.append(
                    f"| {symbol_market} | {_safe_text(c.perilla_node)} | {_safe_text(c.main_dish)} | "
                    f"{_safe_int(c.total_score)}/12 | {_safe_text(c.bottleneck_logic)} | {risk_text} | {evidence_text} |"
                )
        else:
            # 其他分类：简化表格
            lines.append("| 标的(代码/市场) | 环节 | 评分 | 分类原因/备注 |")
            lines.append("|---|---|---|---|")
            for c in cls_candidates:
                clean_name = _safe_text(c.company_name).split("(")[0].split("（")[0].strip()
                symbol = _safe_text(c.symbol)
                if symbol:
                    symbol_market = f"{clean_name}({symbol})"
                else:
                    symbol_market = clean_name
                reason = _safe_text(c.fake_reason) or _safe_text(c.bottleneck_logic)
                lines.append(
                    f"| {symbol_market} | {_safe_text(c.perilla_node)} | {_safe_int(c.total_score)}/12 | {reason} |"
                )
        lines.append("")

    # ── 最有信心的 1-3 个 ──
    if top_picks:
        lines.append("## 最有信心的标的")
        lines.append("")
        for i, pick in enumerate(top_picks, 1):
            clean_pick_name = _safe_text(pick.company_name).split("(")[0].split("（")[0].strip()
            lines.append(f"### {i}. {clean_pick_name} ({_safe_text(pick.symbol)})")
            lines.append(f"- **环节**: {_safe_text(pick.perilla_node)}")
            lines.append(f"- **评分**: {_safe_int(pick.total_score)}/12")
            lines.append(f"- **瓶颈逻辑**: {_safe_text(pick.bottleneck_logic)}")
            lines.append(f"- **压力测试**: {_safe_text(pick.stress_test)}")
            risks = text_list(pick.red_team_risks)
            lines.append(f"- **风险**: {'；'.join(risks) if risks else '暂无'}")
            lines.append(f"- **六问明细**: {_format_scores(pick.six_question_scores)}")
            # 六问推理过程
            reasoning = pick.six_question_reasoning if isinstance(pick.six_question_reasoning, dict) else {}
            if reasoning:
                lines.append("- **六问推理**:")
                for key, label in _SIX_QUESTION_LABELS.items():
                    reason = _safe_text(reasoning.get(key, ""))
                    if reason:
                        lines.append(f"  - {label}: {reason}")
            lines.append("")

    # ── 被淘汰的节点（有评分但未入池，去重） ──
    # 只显示节点级记录（company_name为空），公司级记录太细碎
    eliminated_scored = []
    seen_nodes: set = set()
    for c in eliminated:
        if _safe_bool(c.is_fake_perilla) or not _safe_text(c.perilla_node):
            continue
        # 只保留节点级记录（company_name为空，代表整个环节被淘汰）
        if _safe_text(c.company_name):
            continue
        perilla_node = _safe_text(c.perilla_node)
        if perilla_node in seen_nodes:
            continue
        seen_nodes.add(perilla_node)
        eliminated_scored.append(c)
    if eliminated_scored:
        lines.append("## 被淘汰的环节（非伪紫苏叶）")
        lines.append("")
        lines.append("| 环节 | 评分 | 淘汰原因 | 瓶颈逻辑 |")
        lines.append("|---|---|---|---|")
        for c in eliminated_scored:
            reason = ""
            if _safe_bool(c.pricing_vetoed):
                reason = "定价状态=0（已被市场定价，紫苏叶变牛肉）"
            elif _safe_int(c.total_score) < _CANDIDATE_MIN_SCORE:
                reason = f"评分{_safe_int(c.total_score)}<{_CANDIDATE_MIN_SCORE}"
            elif not _safe_bool(c.stress_test_passed):
                reason = "压力测试未通过"
            else:
                reason = _safe_text(c.fake_reason) or "综合条件不满足"
            lines.append(f"| {_safe_text(c.perilla_node)} | {_safe_int(c.total_score)}/12 | {reason} | {_safe_text(c.bottleneck_logic)} |")
        lines.append("")

    # ── 伪紫苏叶 ──
    fakes = [c for c in eliminated if _safe_bool(c.is_fake_perilla)]
    if fakes:
        lines.append("## ⚠️ 伪紫苏叶（无供货证据，已剔除）")
        lines.append("")
        lines.append("| 标的 | 环节 | 原因 |")
        lines.append("|---|---|---|")
        for c in fakes:
            cn = _safe_text(c.company_name).split("(")[0].split("（")[0].strip()
            lines.append(f"| {cn}({_safe_text(c.symbol)}) | {_safe_text(c.perilla_node)} | {_safe_text(c.fake_reason)} |")
        lines.append("")

    # ── 免责声明 ──
    lines.append("---")
    lines.append("")
    lines.append(disclaimer)

    return "\n".join(lines)


def _format_scores(scores: Dict[str, int]) -> str:
    """格式化六问评分."""
    if not isinstance(scores, dict):
        scores = {}
    parts = []
    for key, label in _SIX_QUESTION_LABELS.items():
        val = max(0, min(_MAX_SCORE_PER_QUESTION, _safe_int(scores.get(key, 0))))
        parts.append(f"{label}={val}")
    return "，".join(parts)
