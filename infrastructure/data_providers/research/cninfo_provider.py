"""CNINFO data provider for listed-company announcements and periodic reports."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from infrastructure.data_providers.base import BaseMarketDataProvider

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

_CNINFO_SESSION = requests.Session() if requests is not None else None
if _CNINFO_SESSION is not None:
    _CNINFO_SESSION.trust_env = False


_CNINFO_SEARCH_URL = "http://www.cninfo.com.cn/new/fulltextSearch/full"
_CNINFO_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "http://www.cninfo.com.cn/new/index",
}


class CninfoProvider(BaseMarketDataProvider):
    """CNINFO provider for announcement full-text search and PDF links."""

    name = "cninfo"

    def is_available(self) -> bool:
        return requests is not None

    def health(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.is_available(),
            "last_error": "",
            "capabilities": ["announcements", "periodic_reports"],
        }

    def get_announcements(self, code: str, count: int = 10) -> list[dict[str, Any]]:
        """Fetch announcement list by stock code from CNINFO full-text search."""
        rows = self._search_announcements(code=code, page_size=count)
        result: list[dict[str, Any]] = []
        for item in rows[:count]:
            result.append(
                {
                    "title": _clean_title(item.get("shortTitle") or item.get("announcementTitle", "")),
                    "date": _format_cninfo_time(item.get("announcementTime", 0)),
                    "code": item.get("secCode", _clean_code(code)),
                    "name": item.get("secName", ""),
                    "pdf_url": _pdf_url(item.get("adjunctUrl", "")),
                    "source": self.name,
                }
            )
        return result

    def search_announcements(
        self, search_query: str, count: int = 10
    ) -> list[dict[str, Any]]:
        """Search CNINFO full text and return normalized announcement rows.

        ``search_query`` may contain both a six-digit security code and a
        research term. This avoids downloading every recent announcement only
        to discover that none of the PDFs contains the requested relationship.
        """
        rows = self._search_announcements(
            code=str(search_query or "").strip(), page_size=max(1, int(count))
        )
        expected_match = re.search(r"\d{6}", str(search_query or ""))
        expected_code = expected_match.group(0) if expected_match else ""
        result: list[dict[str, Any]] = []
        for item in rows[: max(1, int(count))]:
            code = str(item.get("secCode") or "")
            if expected_code and code != expected_code:
                continue
            result.append(
                {
                    "title": _clean_title(
                        item.get("shortTitle") or item.get("announcementTitle", "")
                    ),
                    "date": _format_cninfo_time(item.get("announcementTime", 0)),
                    "code": code or expected_code,
                    "name": item.get("secName", ""),
                    "pdf_url": _pdf_url(item.get("adjunctUrl", "")),
                    "source": self.name,
                    "targeted_search": True,
                }
            )
        return result

    def get_periodic_reports(
        self,
        code: str,
        report_types: list[str] | None = None,
        count: int = 10,
    ) -> list[dict[str, Any]]:
        """Fetch annual, semiannual and quarterly reports by filtering CNINFO titles."""
        wanted = _normalize_report_types(report_types)
        rows = self._search_announcements(code=code, page_size=max(count * 30, 200))
        result: list[dict[str, Any]] = []
        for item in rows:
            title = _clean_title(item.get("shortTitle") or item.get("announcementTitle", ""))
            report_type = _classify_periodic_report(title)
            if not report_type or report_type not in wanted:
                continue
            result.append(
                {
                    "title": title,
                    "date": _format_cninfo_time(item.get("announcementTime", 0)),
                    "code": item.get("secCode", _clean_code(code)),
                    "name": item.get("secName", ""),
                    "pdf_url": _pdf_url(item.get("adjunctUrl", "")),
                    "report_type": report_type,
                    "source": self.name,
                }
            )
            if len(result) >= count:
                break
        return result

    def _search_announcements(self, code: str, page_size: int) -> list[dict[str, Any]]:
        if not self.is_available():
            return []
        try:
            resp = _CNINFO_SESSION.post(  # type: ignore[union-attr]
                _CNINFO_SEARCH_URL,
                data={
                    "searchkey": _clean_code(code),
                    "sdate": "",
                    "edate": "",
                    "isfulltext": "false",
                    "sortName": "pubdate",
                    "sortType": "desc",
                    "pageNum": 1,
                    "pageSize": page_size,
                },
                timeout=10,
                headers=_CNINFO_HEADERS,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            rows = data.get("announcements") or []
            return rows if isinstance(rows, list) else []
        except Exception as exc:
            logger.warning("cninfo search failed: %s", exc)
            return []


def _clean_code(code: str) -> str:
    return re.sub(r"\.(SH|SZ|BJ)$", "", str(code), flags=re.IGNORECASE)


def _clean_title(title: Any) -> str:
    return re.sub(r"<[^>]+>", "", str(title or "")).strip()


def _format_cninfo_time(value: Any) -> str:
    try:
        if value:
            return datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d")
    except Exception:
        pass
    return ""


def _pdf_url(adjunct_url: Any) -> str:
    suffix = str(adjunct_url or "").strip()
    return f"http://static.cninfo.com.cn/{suffix}" if suffix else ""


def _normalize_report_types(report_types: list[str] | None) -> set[str]:
    if not report_types:
        return {"annual_report", "semiannual_report", "quarterly_report"}
    aliases = {
        "annual": "annual_report",
        "year": "annual_report",
        "yearly": "annual_report",
        "annual_report": "annual_report",
        "semiannual": "semiannual_report",
        "halfyear": "semiannual_report",
        "half_year": "semiannual_report",
        "semiannual_report": "semiannual_report",
        "quarter": "quarterly_report",
        "quarterly": "quarterly_report",
        "quarterly_report": "quarterly_report",
        "\u5e74\u62a5": "annual_report",
        "\u534a\u5e74\u62a5": "semiannual_report",
        "\u5b63\u62a5": "quarterly_report",
        "\u4e00\u5b63\u62a5": "quarterly_report",
        "\u4e09\u5b63\u62a5": "quarterly_report",
    }
    normalized: set[str] = set()
    for item in report_types:
        raw = str(item).strip()
        if raw:
            normalized.add(aliases.get(raw.lower(), aliases.get(raw, raw)))
    return normalized


def _classify_periodic_report(title: str) -> str:
    if not title:
        return ""
    if "\u6458\u8981" in title or "\u53d6\u6d88" in title:
        return ""
    if "\u534a\u5e74\u5ea6\u62a5\u544a" in title or "\u534a\u5e74\u62a5" in title:
        return "semiannual_report"
    if (
        "\u5b63\u5ea6\u62a5\u544a" in title
        or "\u4e00\u5b63\u62a5" in title
        or "\u4e09\u5b63\u62a5" in title
    ):
        return "quarterly_report"
    if "\u5e74\u5ea6\u62a5\u544a" in title or "\u5e74\u62a5" in title:
        return "annual_report"
    return ""
