"""板块数据接入。

数据源优先级（细粒度优先）：
1. 东方财富概念板块（通过 AkShareProvider）—— 半导体/航天军工/创新药等细分主题
2. 申万（SW）行业分类（通过 Tushare pro.index_classify）—— 一级行业
3. stock_basic.industry 字段 —— 最终回退

概念板块适合主题轮动；申万行业适合广度判断；stock_basic 仅兜底。
板块指数数据从 index_daily 库读取（已合并的 ArcticDB/LanceDB 库）。

============================================================================
【备用 TODO —— 待接入更细粒度板块分类】
============================================================================

当前数据源能力实测（2026-07-12）：

1. 东方财富概念板块（首选，细分主题）
   - 状态：代码已接入（_load_concept_boards），但当前网络环境被代理/防火墙拦截
   - 错误：ProxyError: Unable to connect to proxy（push2.eastmoney.com 不可达）
   - 修复方向：配置可用代理或切换网络环境后自动恢复

2. 通达信（TDX）板块分类 —— 【待接入，目前不可行】
   - 状态：不可行
   - 原因：TDX 板块数据只在客户端本地（T0002/block_gather.dat），
     服务器端 get_block_info_meta 返回 size=0，不提供下载
   - 阻断点：
     a) pytdx 标准协议（7709）：服务器板块文件 size=0
     b) pytdx 扩展协议（7727/7729）：所有扩展服务器端口不可达
     c) mootdx Affair：tdxpy 服务器 IP Address bad
     d) 本地 block_gather.dat：本机未安装通达信客户端
   - 修复方向（任选其一）：
     a) 安装通达信客户端，下载板块数据后读取本地 block_gather.dat
     b) 找到支持扩展协议的 TDX 服务器（7727/7729 端口）
     c) 使用 pytdx_ext 库（已停止维护，不推荐）

3. 同花顺（THS）概念板块 —— 【待接入，缺成分股接口】
   - 状态：部分可用
   - 原因：akshare 提供 THS 板块名称列表（374 个概念），
     但不提供成分股接口（stock_board_concept_cons_ths 不存在）
   - Tushare 的 ths_index/ths_daily 接口需要更高积分，当前无权限
   - 修复方向（任选其一）：
     a) 爬同花顺网站 basic.10jqka.com.cn 获取成分股（反爬严，不推荐）
     b) 安装 pywencai 走问财接口（自然语言查询，不适合批量初始化）
     c) 升级 Tushare 积分获取 ths_index 接口权限

4. 申万二级行业（L2）—— 【待验证积分是否足够】
   - 状态：代码已接入（_try_load_sw_from_tushare 支持 level='L2'）
   - 当前默认用 L1（一级行业，31 个板块，"通信"太粗）
   - 修复方向：验证 Tushare 积分是否支持 L2，若支持则切换默认级别

============================================================================
临时方案：
- 主路径用 stock_basic.industry（全覆盖，但分类粗，如"通信设备"）
- 补充路径用 config/custom_sector_mapping.yaml（手动维护新概念成分股）
- 实验性趋同验证：通过 sector_source 标记追踪预设板块的走势趋同性

待更细粒度的板块分类（东财概念/TDX/THS）接入后，切换 sector_type 即可，
不影响上层 Pipeline 和 SignalGenerator 的板块匹配逻辑（已用 symbol 反查解耦）。
============================================================================
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# 申万行业分类级别
SW_LEVEL_L1 = "L1"
SW_LEVEL_L2 = "L2"

# 默认板块类型
# 【备用 TODO】东财概念/TDX/同花顺接入后，可切回 SECTOR_TYPE_CONCEPT 或新增 SECTOR_TYPE_TDX/SECTOR_TYPE_THS
# 当前网络环境下东财概念不可达，Pipeline 默认走 basic（stock_basic.industry）
SECTOR_TYPE_CONCEPT = "concept"   # 东方财富概念板块（网络阻断时返回空）
SECTOR_TYPE_SW = "sw"             # 申万行业
SECTOR_TYPE_BASIC = "basic"       # stock_basic.industry 兜底


def load_sector_map(
    data_query: Any | None = None,
    *,
    sector_type: str = SECTOR_TYPE_CONCEPT,
    min_stocks: int = 5,
    max_boards: int = 200,
    keyword_filter: list[str] | None = None,
) -> dict[str, list[str]]:
    """加载板块映射（细粒度优先）。

    Args:
        data_query: 数据查询对象，用于回退到 stock_basic.industry。
        sector_type: 板块类型：
            - "concept": 东方财富概念板块（默认，细分主题）
            - "sw": 申万行业分类
            - "basic": stock_basic.industry 兜底
            - "auto": 按 concept → sw → basic 顺序回退
        min_stocks: 板块最少成分股数，低于此数的板块被过滤。
        max_boards: 概念板块模式下最多加载的板块数（防止过度拉取）。
        keyword_filter: 仅保留板块名包含任一关键词的板块（如 ["半导体","航天","医药"]）。
            None 表示不过滤。

    Returns:
        {板块名: [股票代码列表]} 映射。
    """
    if sector_type == "auto":
        for loader, name in (
            (lambda: _load_concept_boards(
                min_stocks=min_stocks, max_boards=max_boards,
                keyword_filter=keyword_filter), "concept"),
            (lambda: _try_load_sw_from_tushare(SW_LEVEL_L1, min_stocks), "sw"),
            (lambda: _load_from_stock_basic(data_query, min_stocks), "basic"),
        ):
            try:
                sector_map = loader()
                if sector_map:
                    logger.info("板块映射使用 %s，共 %d 个板块", name, len(sector_map))
                    return sector_map
            except Exception as exc:
                logger.warning("加载 %s 板块失败: %s", name, exc)
        return {}

    if sector_type == SECTOR_TYPE_CONCEPT:
        return _load_concept_boards(
            min_stocks=min_stocks,
            max_boards=max_boards,
            keyword_filter=keyword_filter,
        )
    if sector_type == SECTOR_TYPE_SW:
        return _try_load_sw_from_tushare(SW_LEVEL_L1, min_stocks)
    if sector_type == SECTOR_TYPE_BASIC:
        return _load_from_stock_basic(data_query, min_stocks)
    logger.warning("未知 sector_type=%s，回退到 auto", sector_type)
    return load_sector_map(data_query, sector_type="auto", min_stocks=min_stocks,
                           max_boards=max_boards, keyword_filter=keyword_filter)


# 保持向后兼容：旧接口签名
def load_sw_sector_map(
    data_query: Any,
    level: str = SW_LEVEL_L1,
    min_stocks: int = 5,
) -> dict[str, list[str]]:
    """加载申万行业板块映射（向后兼容）。

    优先通过 Tushare pro.index_classify + pro.index_member 获取申万分类。
    若 Tushare 不可用或积分不足，回退到 stock_basic.industry 字段。

    Args:
        data_query: 数据查询对象，需支持 get_stock_basic() 方法。
        level: 申万分类级别，L1（一级）或 L2（二级）。
        min_stocks: 板块最少成分股数。

    Returns:
        {板块名: [股票代码列表]} 映射。
    """
    sw_map = _try_load_sw_from_tushare(level, min_stocks)
    if sw_map:
        logger.info("使用申万 %s 行业分类，共 %d 个板块", level, len(sw_map))
        return sw_map
    fallback_map = _load_from_stock_basic(data_query, min_stocks)
    if fallback_map:
        logger.warning(
            "申万分类不可用，回退到 stock_basic.industry，共 %d 个板块",
            len(fallback_map),
        )
    return fallback_map


# ===========================================================================
# 东方财富概念板块（首选，细分主题）
# ===========================================================================

def _load_concept_boards(
    *,
    min_stocks: int = 5,
    max_boards: int = 200,
    keyword_filter: list[str] | None = None,
) -> dict[str, list[str]]:
    """通过 AkShareProvider 加载东方财富概念板块。

    Args:
        min_stocks: 最少成分股数。
        max_boards: 最多加载板块数。
        keyword_filter: 板块名关键词过滤（None 不过滤）。

    Returns:
        {板块名: [股票代码列表]} 映射，失败返回空 dict。
    """
    try:
        from infrastructure.data_providers.market.akshare_provider import AkShareProvider
    except ImportError:
        logger.debug("AkShareProvider 未安装，跳过概念板块")
        return {}

    try:
        provider = AkShareProvider()
        if not provider.is_available():
            logger.debug("AkShareProvider 不可用，跳过概念板块")
            return {}

        boards_df = provider.get_concept_boards()
        if boards_df is None or boards_df.empty:
            return {}

        sector_map: dict[str, list[str]] = {}
        loaded = 0
        for _, row in boards_df.iterrows():
            if loaded >= max_boards:
                break
            board_name = str(row.get("board_name", "")).strip()
            if not board_name:
                continue
            # 关键词过滤
            if keyword_filter:
                if not any(kw in board_name for kw in keyword_filter):
                    continue
            try:
                members_df = provider.get_concept_board_members(board_name)
                if members_df is None or members_df.empty:
                    continue
                codes = [
                    str(c).strip().zfill(6)
                    for c in members_df.get("symbol", pd.Series(dtype=str)).tolist()
                    if str(c).strip()
                ]
                codes = [c for c in codes if c.isdigit() and len(c) == 6]
                if len(codes) >= min_stocks:
                    sector_map[board_name] = codes
                    loaded += 1
            except Exception as exc:
                logger.debug("概念板块 %s 成分股获取失败: %s", board_name, exc)
                continue

        if sector_map:
            logger.info(
                "加载东方财富概念板块 %d 个（min_stocks=%d, filter=%s）",
                len(sector_map), min_stocks, keyword_filter,
            )
        return sector_map
    except Exception as exc:
        logger.warning("东方财富概念板块加载失败: %s", exc)
        return {}


# ===========================================================================
# 申万行业分类（回退）
# ===========================================================================

def _try_load_sw_from_tushare(level: str, min_stocks: int) -> dict[str, list[str]]:
    """尝试从 Tushare 获取申万行业分类。

    Args:
        level: 申万分类级别。
        min_stocks: 最少成分股数。

    Returns:
        板块映射，失败返回空 dict。
    """
    try:
        from config.secrets import Secrets
        import tushare as ts

        token = Secrets.TUSHARE_TOKEN
        if not token:
            return {}

        pro = ts.pro_api(token)
        classify_df = pro.index_classify(level=level, src="SW2021")
        if classify_df is None or classify_df.empty:
            return {}

        sector_map: dict[str, list[str]] = {}
        for _, row in classify_df.iterrows():
            index_code = str(row.get("index_code", ""))
            industry_name = str(row.get("industry_name", ""))
            if not index_code or not industry_name:
                continue
            member_df = pro.index_member(index_code=index_code)
            if member_df is None or member_df.empty:
                continue
            codes = [
                str(c).strip()
                for c in member_df["con_code"].tolist()
                if str(c).strip()
            ]
            if len(codes) >= min_stocks:
                sector_map[industry_name] = codes

        return sector_map
    except ImportError:
        logger.debug("tushare 未安装，跳过申万分类")
        return {}
    except Exception as exc:
        logger.debug("申万分类获取失败: %s", exc)
        return {}


# ===========================================================================
# stock_basic.industry 兜底
# ===========================================================================

def _load_from_stock_basic(data_query: Any, min_stocks: int) -> dict[str, list[str]]:
    """从 stock_basic.industry 加载行业映射（最终回退）。

    Args:
        data_query: 数据查询对象。
        min_stocks: 最少成分股数。

    Returns:
        板块映射。
    """
    if data_query is None:
        return {}
    try:
        basic_df = data_query.get_stock_basic()
    except Exception as exc:
        logger.warning("get_stock_basic 失败: %s", exc)
        return {}

    if basic_df is None or (hasattr(basic_df, "empty") and basic_df.empty):
        return {}

    if hasattr(basic_df, "to_pandas"):
        basic_df = basic_df.to_pandas()

    code_col = _find_column(basic_df, ["stock_code", "symbol_code", "symbol", "ts_code", "code"])
    industry_col = _find_column(basic_df, ["industry", "sector", "sw_industry", "industry_name"])

    if not code_col or not industry_col:
        return {}

    basic_df = basic_df[[code_col, industry_col]].dropna()
    basic_df[industry_col] = basic_df[industry_col].astype(str).str.strip()
    basic_df = basic_df[basic_df[industry_col].replace("", pd.NA).notna()]

    sector_map: dict[str, list[str]] = {}
    for _, row in basic_df.iterrows():
        code = str(row[code_col]).strip()
        sector = str(row[industry_col]).strip()
        if not code or not sector:
            continue
        sector_map.setdefault(sector, []).append(code)

    return {s: codes for s, codes in sector_map.items() if len(codes) >= min_stocks}


# ===========================================================================
# 工具
# ===========================================================================

def _find_column(df: pd.DataFrame, candidates: list[str]) -> str:
    """在 DataFrame 中查找匹配的列名。

    Args:
        df: 目标 DataFrame。
        candidates: 候选列名列表。

    Returns:
        匹配的列名，未找到返回空串。
    """
    columns = set(df.columns)
    for c in candidates:
        if c in columns:
            return c
    return ""
