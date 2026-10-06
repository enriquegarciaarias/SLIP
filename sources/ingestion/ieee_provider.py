#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ieee_provider.py
================

Proveedor de IEEE Xplore basado en su Metadata API:
    https://ieeexploreapi.ieee.org/api/v1/search/articles

Devuelve metadatos + abstract de artículos de revistas y conferencias. El
`article_number` (arnumber) se usa para construir la landing page correcta,
a diferencia del parser de fichero, que usaba el sufijo del DOI.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sources.common.common import logger, writeLog
from sources.common.utils import safe_int
from sources.ingestion.base import CanonicalBuilder, MissingCredentials, SearchProvider

IEEE_SEARCH_URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
IEEE_MAX_PAGE = 200


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class IeeeProvider(SearchProvider):
    def _api_key(self) -> str:
        api_key = self.config.get("api_key")
        if not api_key:
            raise MissingCredentials("IEEE requiere 'search.providers.ieee.api_key'")
        return api_key

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------
    def fetch(self, query: str, *, max_results: int = 500) -> Dict[str, Any]:
        api_key = self._api_key()
        page_size = min(IEEE_MAX_PAGE, int(self.config.get("max_records", IEEE_MAX_PAGE)))

        articles: List[Dict[str, Any]] = []
        total: Optional[int] = None
        start_record = 1

        while len(articles) < max_results:
            remaining = max_results - len(articles)
            params = {
                "apikey": api_key,
                "format": "json",
                "querytext": query,
                "max_records": min(page_size, remaining),
                "start_record": start_record,
            }
            data = self.client.get(IEEE_SEARCH_URL, params=params)
            total = safe_int(data.get("total_records")) or total
            page = [a for a in _as_list(data.get("articles")) if isinstance(a, dict)]

            if not page:
                break
            articles.extend(page)
            start_record += len(page)
            if total is not None and start_record > total:
                break

        articles = articles[:max_results]
        writeLog("info", logger, f"[IEEE] búsqueda devolvió {len(articles)} artículos (total={total})")
        return {"query": query, "total": total, "articles": articles}

    # ------------------------------------------------------------------
    # normalize
    # ------------------------------------------------------------------
    def normalize(self, raw: Any) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for article in raw.get("articles", []):
            record = self._parse_article(article)
            if record and record.get("title"):
                records.append(record)
        return records

    def _parse_article(self, article: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        arnumber = str(article.get("article_number") or "").strip()
        title = article.get("title") or ""
        abstract = article.get("abstract") or ""
        doi = article.get("doi") or ""
        year = safe_int(article.get("publication_year"))
        venue = article.get("publication_title") or ""
        document_type = article.get("content_type") or article.get("document_identifier") or ""
        citation_count = safe_int(article.get("citing_paper_count"))
        references_count = safe_int(article.get("reference_count"))

        authors: List[str] = []
        authors_detail: List[Dict[str, Any]] = []
        for author in _as_list((article.get("authors", {}) or {}).get("authors")):
            if not isinstance(author, dict):
                continue
            name = author.get("full_name") or author.get("author_name") or ""
            if not name:
                continue
            authors.append(name)
            authors_detail.append(
                {"name": name, "id": author.get("id"), "affiliation": author.get("affiliation")}
            )

        keywords: List[str] = []
        for term in _as_list((article.get("author_terms", {}) or {}).get("terms")):
            if term:
                keywords.append(str(term).strip())
        ieee_terms: List[str] = []
        for term in _as_list((article.get("ieee_terms", {}) or {}).get("terms")):
            if term:
                ieee_terms.append(str(term).strip())
        keywords.extend(ieee_terms)

        pdf_url = article.get("pdf_url")
        html_url = article.get("html_url")
        landing_page = html_url or (
            f"https://ieeexplore.ieee.org/document/{arnumber}" if arnumber else None
        )

        return CanonicalBuilder.build(
            source=self.source,
            source_id=arnumber,
            doi=doi,
            title=title,
            abstract=abstract,
            authors=authors,
            authors_detail=authors_detail,
            keywords=keywords,
            year=year,
            venue=venue,
            document_type=document_type,
            language=article.get("language") or "",
            references_count=references_count,
            citation_count=citation_count,
            pdf_url=pdf_url,
            landing_page=landing_page,
            open_access=article.get("access_type"),
            source_url=landing_page,
            extra={"ieee_terms": ieee_terms, "arnumber": arnumber},
        )
