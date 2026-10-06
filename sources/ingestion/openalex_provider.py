#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
openalex_provider.py
====================

Proveedor de OpenAlex (https://api.openalex.org): catálogo abierto y gratuito
de trabajos académicos que incluye preprints y metadatos enriquecidos. No
requiere API key; se recomienda configurar `email` para entrar en el
denominado "polite pool" (mayor cuota y prioridad).

OpenAlex devuelve el abstract como `abstract_inverted_index` (mapa palabra ->
posiciones), por lo que se reconstruye el texto original.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sources.common.common import logger, writeLog
from sources.common.utils import safe_int
from sources.ingestion.base import CanonicalBuilder, SearchProvider

WORKS_URL = "https://api.openalex.org/works"
MAX_PER_PAGE = 200


def _reconstruct_abstract(inverted: Optional[Dict[str, List[int]]]) -> str:
    """Reconstruye el abstract a partir del índice invertido de OpenAlex."""
    if not inverted:
        return ""
    positions: List[tuple] = []
    for word, pos_list in inverted.items():
        for pos in pos_list or []:
            positions.append((pos, word))
    positions.sort(key=lambda item: item[0])
    return " ".join(word for _, word in positions)


def _strip_id(value: str) -> str:
    return (value or "").rstrip("/").split("/")[-1]


class OpenAlexProvider(SearchProvider):
    def _mailto(self) -> str:
        return (self.config.get("email") or "").strip()

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------
    def fetch(self, query: str, *, max_results: int = 500) -> Dict[str, Any]:
        per_page = min(MAX_PER_PAGE, max(1, int(self.config.get("page_size", MAX_PER_PAGE))))
        results: List[Dict[str, Any]] = []
        total: Optional[int] = None
        page = 1

        while len(results) < max_results:
            params: Dict[str, Any] = {
                "search": query,
                "per-page": per_page,
                "page": page,
                "sort": self.config.get("sort", "relevance_score:desc"),
            }
            mailto = self._mailto()
            if mailto:
                params["mailto"] = mailto

            data = self.client.get(WORKS_URL, params=params)
            batch = data.get("results", []) or []
            total = safe_int((data.get("meta", {}) or {}).get("count")) or total
            if not batch:
                break
            results.extend(batch)
            if len(batch) < per_page:
                break
            page += 1

        results = results[:max_results]
        writeLog("info", logger, f"[OpenAlex] búsqueda devolvió {len(results)} trabajos (total={total})")
        return {"query": query, "total": total, "results": results}

    # ------------------------------------------------------------------
    # normalize
    # ------------------------------------------------------------------
    def normalize(self, raw: Any) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for work in raw.get("results", []) or []:
            record = self._parse_work(work)
            if record and record.get("title"):
                records.append(record)
        return records

    def _parse_work(self, work: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        ids = work.get("ids", {}) or {}
        title = work.get("title") or work.get("display_name") or ""
        abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))

        authors: List[str] = []
        authors_detail: List[Dict[str, Any]] = []
        for authorship in work.get("authorships", []) or []:
            author = (authorship or {}).get("author", {}) or {}
            name = (author.get("display_name") or "").strip()
            if name and name not in authors:
                authors.append(name)
                authors_detail.append({
                    "name": name,
                    "id": _strip_id(author.get("id", "")),
                    "orcid": author.get("orcid"),
                })

        keywords: List[str] = []
        for keyword in work.get("keywords", []) or []:
            if isinstance(keyword, dict) and keyword.get("display_name"):
                keywords.append(keyword["display_name"])
        for topic in work.get("topics", []) or []:
            if isinstance(topic, dict) and topic.get("display_name"):
                keywords.append(topic["display_name"])

        primary = work.get("primary_location") or {}
        source_info = (primary.get("source") or {}) if isinstance(primary, dict) else {}
        venue = source_info.get("display_name", "") if isinstance(source_info, dict) else ""
        landing_page = primary.get("landing_page_url") if isinstance(primary, dict) else None
        pdf_url = primary.get("pdf_url") if isinstance(primary, dict) else None

        oa = work.get("open_access") or {}
        is_oa = oa.get("is_oa") if isinstance(oa, dict) else None
        oa_url = oa.get("oa_url") if isinstance(oa, dict) else None

        pmid = _strip_id(ids["pmid"]) if ids.get("pmid") else ""
        pmcid = _strip_id(ids["pmcid"]) if ids.get("pmcid") else ""

        return CanonicalBuilder.build(
            source=self.source,
            source_id=_strip_id(work.get("id", "")),
            doi=work.get("doi") or "",
            title=title,
            abstract=abstract,
            authors=authors,
            authors_detail=authors_detail,
            keywords=keywords,
            year=safe_int(work.get("publication_year")),
            venue=venue,
            document_type=work.get("type", "") or "",
            language=work.get("language", "") or "",
            references_count=safe_int(work.get("referenced_works_count")),
            citation_count=safe_int(work.get("cited_by_count")),
            pmid=pmid,
            pmcid=pmcid,
            oa_url=oa_url or "",
            pdf_url=pdf_url,
            landing_page=landing_page,
            open_access="open" if is_oa else ("closed" if is_oa is False else None),
            source_url=work.get("id", ""),
        )
