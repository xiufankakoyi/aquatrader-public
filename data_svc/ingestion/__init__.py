"""数据摄入层：提供多源同步、归一化、去重与事件存储能力。"""

from .crawler_etiquette import CrawlerEtiquette
from .document_store import DocumentStore
from .event_deduper import EventDeduper
from .multisource_ingestion import MultiSourceIngestion
from .normalizer import Normalizer
from .watermark_manager import WatermarkManager

__all__ = [
    "CrawlerEtiquette",
    "DocumentStore",
    "EventDeduper",
    "MultiSourceIngestion",
    "Normalizer",
    "WatermarkManager",
]
