"""规则引擎抽取器 — 基于关键词匹配提取主题、风险、催化剂、护城河、财务信号。"""

from __future__ import annotations

import hashlib
import logging
from typing import Dict, List

from core.events.models import ExtractionResult

logger = logging.getLogger(__name__)


class RuleBasedExtractor:
    """基于关键词匹配的信息抽取器。

    无 LLM 依赖，始终可用作为兜底方案。
    从文本中提取主题、风险、催化剂、护城河声明、财务信号，
    并附带原文证据片段。
    """

    THEME_KEYWORDS: Dict[str, List[str]] = {
        "AI": ["AI", "人工智能", "大模型", "GPT", "LLM", "AIGC", "智能体", "Agent"],
        "算力": ["算力", "GPU", "服务器", "IDC", "智算", "算力中心", "训练"],
        "光模块": ["光模块", "CPO", "硅光", "光通信", "800G", "400G"],
        "存储": ["存储", "HBM", "DRAM", "NAND", "SSD", "内存"],
        "机器人": ["机器人", "人形机器人", "工业机器人", "减速器", "伺服", "灵巧手"],
        "低空经济": ["低空经济", "eVTOL", "无人机", "飞行汽车", "低空"],
        "电力": ["电力", "电网", "特高压", "光伏", "风电", "储能", "虚拟电厂"],
        "数据中心": ["数据中心", "IDC", "液冷", "温控", "PUE"],
        "半导体": ["半导体", "芯片", "晶圆", "光刻", "EDA", "封测", "国产替代"],
        "消费电子": ["消费电子", "MR", "VR", "AR", "折叠屏", "智能穿戴"],
        "汽车电子": ["汽车电子", "智能驾驶", "激光雷达", "车载", "自动驾驶"],
        "医药": ["医药", "创新药", "CXO", "生物药", "疫苗", "医疗器械"],
        "军工": ["军工", "国防", "军品", "导弹", "航发", "军贸"],
    }

    RISK_KEYWORDS: List[str] = [
        "减持", "亏损", "诉讼", "处罚", "立案", "监管函", "退市风险", "ST",
        "商誉减值", "订单不及预期", "毛利率下降", "现金流压力",
        "风险", "违规", "质押", "冻结", "被执行", "失信", "爆雷",
    ]

    CATALYST_KEYWORDS: List[str] = [
        "中标", "合同", "订单", "产能", "扩产", "涨价", "并购", "回购", "增持",
        "业绩预增", "新产品", "客户突破", "获批", "量产", "交付",
        "突破", "首发", "上线", "签约", "合作",
    ]

    MOAT_KEYWORDS: List[str] = [
        "独家", "龙头", "领先", "高壁垒", "认证", "专利", "客户粘性",
        "长协", "稀缺", "产能锁定", "技术优势", "份额提升", "国产替代",
        "护城河", "垄断", "寡头", "不可替代", "核心供应商",
    ]

    FINANCIAL_SIGNAL_KEYWORDS: Dict[str, List[str]] = {
        "revenue_growth": ["营收增长", "收入增长", "营收同比"],
        "profit_growth": ["利润增长", "净利润增长", "归母净利润增长"],
        "margin_improve": ["毛利率提升", "净利率提升", "毛利率改善"],
        "cash_flow_positive": ["经营现金流", "现金流改善"],
    }

    def extract(
        self,
        title: str,
        content: str,
        symbol: str = "",
        stock_name: str = "",
    ) -> ExtractionResult:
        """从文本中提取结构化信息。

        使用关键词匹配提取主题、风险、催化剂、护城河声明和财务信号，
        并附带原文证据片段。情绪判断基于风险与催化剂的比率。

        Args:
            title: 文档标题
            content: 文档正文
            symbol: 标的代码
            stock_name: 股票名称

        Returns:
            ExtractionResult 抽取结果
        """
        full_text = f"{title} {content}"
        evidence_items: List[Dict] = []

        # 1. 提取主题
        themes = []
        for theme_name, keywords in self.THEME_KEYWORDS.items():
            for kw in keywords:
                ctx = self._find_keyword_context(full_text, kw)
                if ctx:
                    themes.append(theme_name)
                    evidence_items.append({
                        "category": "theme",
                        "label": theme_name,
                        "evidence_text": ctx,
                        "confidence": 0.7,
                    })
                    break  # 每个主题只记录一次

        # 2. 提取风险
        risks = []
        for kw in self.RISK_KEYWORDS:
            ctx = self._find_keyword_context(full_text, kw)
            if ctx:
                risks.append(kw)
                evidence_items.append({
                    "category": "risk",
                    "label": kw,
                    "evidence_text": ctx,
                    "confidence": 0.6,
                })

        # 3. 提取催化剂
        catalysts = []
        for kw in self.CATALYST_KEYWORDS:
            ctx = self._find_keyword_context(full_text, kw)
            if ctx:
                catalysts.append(kw)
                evidence_items.append({
                    "category": "catalyst",
                    "label": kw,
                    "evidence_text": ctx,
                    "confidence": 0.6,
                })

        # 4. 提取护城河声明
        moat_claims = []
        for kw in self.MOAT_KEYWORDS:
            ctx = self._find_keyword_context(full_text, kw)
            if ctx:
                moat_claims.append(kw)
                evidence_items.append({
                    "category": "moat",
                    "label": kw,
                    "evidence_text": ctx,
                    "confidence": 0.5,
                })

        # 5. 提取财务信号
        financial_signals = []
        for signal_name, keywords in self.FINANCIAL_SIGNAL_KEYWORDS.items():
            for kw in keywords:
                ctx = self._find_keyword_context(full_text, kw)
                if ctx:
                    financial_signals.append(signal_name)
                    evidence_items.append({
                        "category": "financial_signal",
                        "label": signal_name,
                        "evidence_text": ctx,
                        "confidence": 0.7,
                    })
                    break  # 每个信号类别只记录一次

        # 6. 情绪判断
        sentiment = self._determine_sentiment(risks, catalysts)

        # 7. 生成摘要
        summary_parts = []
        if themes:
            summary_parts.append(f"主题: {', '.join(themes[:3])}")
        if risks:
            summary_parts.append(f"风险: {', '.join(risks[:3])}")
        if catalysts:
            summary_parts.append(f"催化剂: {', '.join(catalysts[:3])}")
        summary = "; ".join(summary_parts) if summary_parts else "无显著信息"

        # 8. 置信度判断
        confidence = "medium"
        if len(evidence_items) >= 5:
            confidence = "high"
        elif len(evidence_items) <= 1:
            confidence = "low"

        return ExtractionResult(
            symbol=symbol,
            stock_name=stock_name,
            summary=summary[:300],
            themes=themes,
            catalysts=catalysts,
            risks=risks,
            moat_claims=moat_claims,
            financial_signals=financial_signals,
            sentiment=sentiment,
            confidence=confidence,
            evidence_items=evidence_items,
            processor="rule_based",
        )

    @staticmethod
    def _find_keyword_context(text: str, keyword: str, context_chars: int = 50) -> str:
        """在文本中查找关键词并返回上下文片段。

        Args:
            text: 待搜索文本
            keyword: 关键词
            context_chars: 上下文字符数

        Returns:
            包含关键词的上下文片段，未找到返回空字符串
        """
        try:
            idx = text.find(keyword)
            if idx == -1:
                return ""
            start = max(0, idx - context_chars)
            end = min(len(text), idx + len(keyword) + context_chars)
            snippet = text[start:end].strip()
            # 标记关键词位置
            if start > 0:
                snippet = "..." + snippet
            if end < len(text):
                snippet = snippet + "..."
            return snippet
        except Exception:
            return ""

    @staticmethod
    def _determine_sentiment(risks: List[str], catalysts: List[str]) -> str:
        """根据风险和催化剂数量判断情绪倾向。

        Args:
            risks: 风险关键词列表
            catalysts: 催化剂关键词列表

        Returns:
            positive / neutral / negative
        """
        risk_weight = len(risks) * 1.5  # 风险权重更高
        catalyst_weight = len(catalysts)

        if catalyst_weight > risk_weight + 1:
            return "positive"
        if risk_weight > catalyst_weight + 1:
            return "negative"
        return "neutral"
