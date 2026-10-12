"""ArxivProvider - arXiv 学术论文搜索

通过 arxiv Python 库搜索量化金融/机器学习相关论文。
无需 API Key，公开数据源。

常用搜索分类：
- q-fin: Quantitative Finance（量化金融）
- cs.LG: Machine Learning（机器学习）
- stat.ML: Statistics - Machine Learning
"""

from __future__ import annotations

import logging
from typing import Any

import arxiv

logger = logging.getLogger(__name__)

# 量化金融相关默认搜索分类
_FINANCE_CATEGORIES = [
    "q-fin",       # 量化金融总类
    "q-fin.CP",    # 计算金融
    "q-fin.GN",    # 金融总论
    "q-fin.PM",    # 投资组合管理
    "q-fin.ST",    # 统计金融
    "q-fin.TR",    # 交易与做市
]


class ArxivProvider:
    """arXiv 学术论文搜索，支持量化金融/ML 论文检索。"""

    name = "arxiv"

    def is_available(self) -> bool:
        """arxiv 库始终可用（无需 API Key）。"""
        return True

    def health(self) -> dict[str, Any]:
        """返回数据源健康状态。"""
        return {
            "name": self.name,
            "available": True,
            "last_error": "",
            "capabilities": ["search_papers", "search_finance_papers"],
        }

    def search_papers(
        self,
        query: str,
        count: int = 10,
        sort_by: str = "relevance",
        sort_order: str = "descending",
    ) -> list[dict[str, Any]]:
        """搜索 arXiv 论文。

        Args:
            query: 搜索关键词，支持 arXiv 查询语法
                   例: "stock market prediction", "ti:transformer AND cat:q-fin.ST"
            count: 返回结果数（1-50）
            sort_by: 排序方式 "relevance"/"lastUpdatedDate"/"submittedDate"
            sort_order: 排序方向 "descending"/"ascending"

        Returns:
            论文列表 [{title, url, pdf_url, abstract, authors, published, categories, arxiv_id}]
        """
        count = max(1, min(50, count))

        sort_map = {
            "relevance": arxiv.SortCriterion.Relevance,
            "lastUpdatedDate": arxiv.SortCriterion.LastUpdatedDate,
            "submittedDate": arxiv.SortCriterion.SubmittedDate,
        }
        order_map = {
            "descending": arxiv.SortOrder.Descending,
            "ascending": arxiv.SortOrder.Ascending,
        }

        try:
            client = arxiv.Client()
            search = arxiv.Search(
                query=query,
                max_results=count,
                sort_by=sort_map.get(sort_by, arxiv.SortCriterion.Relevance),
                sort_order=order_map.get(sort_order, arxiv.SortOrder.Descending),
            )

            results: list[dict[str, Any]] = []
            for paper in client.results(search):
                results.append({
                    "title": paper.title,
                    "url": paper.entry_id,
                    "pdf_url": paper.pdf_url,
                    "abstract": paper.summary.replace("\n", " ").strip(),
                    "authors": [a.name for a in paper.authors],
                    "published": paper.published.strftime("%Y-%m-%d") if paper.published else "",
                    "updated": paper.updated.strftime("%Y-%m-%d") if paper.updated else "",
                    "categories": paper.categories,
                    "arxiv_id": paper.get_short_id(),
                    "comment": paper.comment or "",
                    "source": self.name,
                })

            return results
        except Exception as exc:
            logger.warning("arXiv 搜索失败 (query=%s): %s", query[:30], exc)
            return []

    def search_finance_papers(
        self,
        keyword: str = "",
        count: int = 10,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """搜索量化金融相关论文（便捷方法）。

        限定 q-fin 子分类，按提交日期降序排列。
        注意：arXiv API 的 cat:q-fin 大类查询不稳定，需用子分类 OR 组合。

        Args:
            keyword: 额外关键词，空则返回所有量化金融论文
            count: 返回结果数
            days: 搜索最近N天的论文（仅文档说明，实际由 arXiv API 排序决定）

        Returns:
            论文列表 [{title, url, pdf_url, abstract, authors, published, categories, arxiv_id}]
        """
        # arXiv 的 cat:q-fin 大类查询不稳定，用子分类 OR 组合
        cat_query = " OR ".join(f"cat:{c}" for c in _FINANCE_CATEGORIES)
        if keyword:
            query = f'ti:"{keyword}" AND ({cat_query})'
        else:
            query = cat_query

        return self.search_papers(
            query=query,
            count=count,
            sort_by="submittedDate",
            sort_order="descending",
        )

    def search_ml_finance_papers(
        self,
        keyword: str = "",
        count: int = 10,
    ) -> list[dict[str, Any]]:
        """搜索 ML+金融交叉领域论文。

        搜索量化金融 + 机器学习/深度学习相关论文。

        Args:
            keyword: 额外关键词
            count: 返回结果数

        Returns:
            论文列表
        """
        if keyword:
            cat_query = " OR ".join(f"cat:{c}" for c in _FINANCE_CATEGORIES)
            query = f'ti:"{keyword}" AND ({cat_query} OR (cat:cs.LG AND abs:finance))'
        else:
            query = " OR ".join(f"cat:{c}" for c in _FINANCE_CATEGORIES)
            query = query + " OR (cat:cs.LG AND abs:finance)"

        return self.search_papers(
            query=query,
            count=count,
            sort_by="submittedDate",
            sort_order="descending",
        )

    def get_paper(self, arxiv_id: str) -> dict[str, Any] | None:
        """获取单篇论文详情。

        Args:
            arxiv_id: arXiv ID，如 "2606.12345" 或 "2301.00001"

        Returns:
            论文详情字典，未找到返回 None
        """
        try:
            client = arxiv.Client()
            search = arxiv.Search(id_list=[arxiv_id])
            for paper in client.results(search):
                return {
                    "title": paper.title,
                    "url": paper.entry_id,
                    "pdf_url": paper.pdf_url,
                    "abstract": paper.summary.replace("\n", " ").strip(),
                    "authors": [a.name for a in paper.authors],
                    "published": paper.published.strftime("%Y-%m-%d") if paper.published else "",
                    "updated": paper.updated.strftime("%Y-%m-%d") if paper.updated else "",
                    "categories": paper.categories,
                    "arxiv_id": paper.get_short_id(),
                    "comment": paper.comment or "",
                    "source": self.name,
                }
            return None
        except Exception as exc:
            logger.warning("arXiv 获取论文失败 (id=%s): %s", arxiv_id, exc)
            return None
