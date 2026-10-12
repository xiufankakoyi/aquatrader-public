"""文本分块器 — 将长文本拆分为 1500-3000 字符的块，保留元数据。"""

from __future__ import annotations

import logging
import re
from typing import Dict, List

logger = logging.getLogger(__name__)

# 句子边界分隔符
_SENTENCE_BREAKS = re.compile(r"(。|！|？|\n)")


class TextChunker:
    """将长文本拆分为带元数据的文本块。

    规则:
    - 优先在句子边界（。！？\\n）处分割
    - 每个块 1500-3000 字符
    - 块之间约 200 字符重叠
    - 每个块保留 title/source/date/symbol 元数据
    """

    def chunk(
        self,
        text: str,
        title: str = "",
        source: str = "",
        date: str = "",
        symbol: str = "",
        chunk_size: int = 2000,
        overlap: int = 200,
    ) -> List[Dict]:
        """将文本拆分为带元数据的块。

        Args:
            text: 待分割的原始文本
            title: 文档标题
            source: 数据来源
            date: 日期
            symbol: 标的代码
            chunk_size: 目标块大小（字符数）
            overlap: 块间重叠字符数

        Returns:
            块字典列表，每个包含 text/title/source/date/symbol/chunk_index/total_chunks
        """
        if not text or not text.strip():
            return []

        # 短文本直接返回
        if len(text) <= chunk_size:
            return [{
                "text": text,
                "title": title,
                "source": source,
                "date": date,
                "symbol": symbol,
                "chunk_index": 0,
                "total_chunks": 1,
            }]

        # 按句子边界分割
        segments = self._split_by_sentences(text)
        chunks = self._merge_segments(segments, chunk_size, overlap)

        # 附加元数据
        total = len(chunks)
        for i, chunk_text in enumerate(chunks):
            chunks[i] = {
                "text": chunk_text,
                "title": title,
                "source": source,
                "date": date,
                "symbol": symbol,
                "chunk_index": i,
                "total_chunks": total,
            }

        return chunks

    @staticmethod
    def _split_by_sentences(text: str) -> List[str]:
        """按句子边界分割文本。

        保留分隔符在句子末尾。

        Args:
            text: 原始文本

        Returns:
            句子片段列表
        """
        # 使用 findall 匹配：每个句子 = 非分隔符* + 分隔符
        # 也匹配末尾无分隔符的片段
        parts = re.findall(r"[^。！？\n]*[。！？\n]?", text)
        # 过滤空片段
        return [p for p in parts if p.strip()]

    @staticmethod
    def _merge_segments(
        segments: List[str],
        chunk_size: int,
        overlap: int,
    ) -> List[str]:
        """将句子片段合并为目标大小的块。

        Args:
            segments: 句子片段列表
            chunk_size: 目标块大小
            overlap: 重叠大小

        Returns:
            合并后的文本块列表
        """
        if not segments:
            return []

        chunks: List[str] = []
        current_parts: List[str] = []
        current_len = 0

        for seg in segments:
            seg_len = len(seg)
            # 如果当前块 + 新片段超过目标大小，且当前块不为空
            if current_len + seg_len > chunk_size and current_parts:
                chunk_text = "".join(current_parts)
                chunks.append(chunk_text)

                # 计算重叠：从当前块末尾取 overlap 字符作为下一块的开头
                if overlap > 0 and len(chunk_text) > overlap:
                    overlap_text = chunk_text[-overlap:]
                    current_parts = [overlap_text]
                    current_len = len(overlap_text)
                else:
                    current_parts = []
                    current_len = 0

            current_parts.append(seg)
            current_len += seg_len

        # 处理剩余片段
        if current_parts:
            remaining = "".join(current_parts)
            # 如果剩余太短且已有块，合并到最后一个块
            if len(remaining) < 500 and chunks:
                chunks[-1] += remaining
            else:
                chunks.append(remaining)

        return chunks
