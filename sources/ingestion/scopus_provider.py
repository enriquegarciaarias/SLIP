#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
scopus_provider.py
==================

Proveedor de Scopus (Elsevier) basado en dos endpoints oficiales:

  1. Scopus Search API      : /content/search/scopus       (metadatos)
  2. Abstract Retrieval API : /content/abstract/scopus_id/{id} (abstract,
     keywords de autor, áreas temáticas y referencias)

Scopus no devuelve el abstract en la búsqueda, por lo que se completa con una
llamada por registro (cacheada en el snapshot). Requiere API key institucional.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sources.common.common import logger, writeLog
from sources.common.utils import safe_int
from sources.ingestion.base import CanonicalBuilder, MissingCredentials, SearchProvider

SEARCH_URL = "https://api.elsevier.com/content/search/scopus"
ABSTRACT_URL = "https://api.elsevier.com/content/abstract/scopus_id"


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _scopus_id(entry: Dict[str, Any]) -> str:
    identifier = entry.get("dc:identifier", "") or ""
    if ":" in identifier:
        return identifier.split(":")[-1]
    return identifier


def _link_href(entry: Dict[str, Any], ref_kind: str) -> Optional[str]:
    for link in _as_list(entry.get("link")):
        if not isinstance(link, dict):
            continue
        ref = link.get("@ref") or link.get("ref")
        href = link.get("@href") or link.get("href")
        if ref == ref_kind and href:
            return href
    return None


def _name_from_author(author: Any) -> tuple[str, Dict[str, Any]]:
    """Extrae nombre + detalle de un autor en los distintos formatos de Scopus."""
    if isinstance(author, str):
        name = author.strip()
        return name, {"name": name}

    if not isinstance(author, dict):
        return "", {}

    name = author.get("authname")
    if name:
        name = name.strip()
        return name, {"name": name, "id": author.get("authid"), "orcid": author.get("orcid")}

    pref = author.get("preferred-name") or {}
    given = author.get("ce:given-name") or pref.get("ce:given-name")
    surname = author.get("ce:surname") or pref.get("ce:surname")
    indexed = author.get("ce:indexed-name") or pref.get("ce:indexed-name")

    if surname and given:
        display = f"{surname}, {given}"
    else:
        display = indexed or surname or given or ""

    return display, {"name": display, "id": author.get("@auid"), "given": given, "surname": surname}


def _collect_authors(retrieval: Dict[str, Any], core: Dict[str, Any], entry: Dict[str, Any]) -> tuple[list, list]:
    """Recoge autores desde la vista META (coredata.dc:creator), el nodo authors o la búsqueda."""
    node: Any = None

    creator = core.get("dc:creator")
    if isinstance(creator, dict):
        node = creator.get("author")
    elif isinstance(creator, str):
        node = creator

    if not node:
        node = (retrieval.get("authors") or {}).get("author")
    if not node:
        node = entry.get("author")
    if not node:
        node = entry.get("dc:creator")

    authors: list = []
    details: list = []
    for author in _as_list(node):
        name, detail = _name_from_author(author)
        if name and name not in authors:
            authors.append(name)
            details.append(detail)
    return authors, details


class ScopusProvider(SearchProvider):
    def _headers(self) -> Dict[str, str]:
        api_key = self.config.get("api_key")
        if not api_key:
            raise MissingCredentials("Scopus requiere 'search.providers.scopus.api_key'")
        headers = {"X-ELS-APIKey": api_key, "Accept": "application/json"}
        if self.config.get("inst_token"):
            headers["X-ELS-Insttoken"] = self.config["inst_token"]
        return headers

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------
    def fetch(self, query: str, *, max_results: int = 500) -> Dict[str, Any]:
        headers = self._headers()
        page_size = min(25, int(self.config.get("page_size", 25)))

        entries: List[Dict[str, Any]] = []
        total: Optional[int] = None
        start = 0

        while len(entries) < max_results:
            params = {
                "query": query,
                "count": page_size,
                "start": start,
                "sort": self.config.get("sort", "-cited-by-count"),
            }
            data = self.client.get(SEARCH_URL, params=params, headers=headers)
            results = data.get("search-results", {})
            total = safe_int(results.get("opensearch:totalResults")) or total
            page = [e for e in _as_list(results.get("entry")) if isinstance(e, dict)]
            page = [e for e in page if "error" not in e]

            if not page:
                break
            entries.extend(page)
            start += page_size
            if total is not None and start >= total:
                break

        entries = entries[:max_results]
        writeLog("info", logger, f"[Scopus] búsqueda devolvió {len(entries)} entradas (total={total})")

        abstracts: Dict[str, Any] = {}
        for entry in entries:
            sid = _scopus_id(entry)
            if not sid:
                continue
            try:
                abstracts[sid] = self.client.get(
                    f"{ABSTRACT_URL}/{sid}",
                    params={
                        "httpAccept": "application/json",
                        "view": self.config.get("abstract_view", "META"),
                    },
                    headers=headers,
                )
            except Exception as exc:
                writeLog("warning", logger, f"[Scopus] abstract no disponible para {sid}: {exc}")

        return {"query": query, "total": total, "entries": entries, "abstracts": abstracts}

    # ------------------------------------------------------------------
    # normalize
    # ------------------------------------------------------------------
    def normalize(self, raw: Any) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        abstracts = raw.get("abstracts", {}) or {}
        for entry in raw.get("entries", []):
            sid = _scopus_id(entry)
            record = self._parse_entry(entry, sid, abstracts.get(sid, {}))
            if record and record.get("title"):
                records.append(record)
        return records

    def _parse_entry(
        self, entry: Dict[str, Any], sid: str, abstract_payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        retrieval = (abstract_payload or {}).get("abstracts-retrieval-response", {}) or {}
        core = retrieval.get("coredata", {}) or {}

        title = entry.get("dc:title") or core.get("dc:title") or ""
        doi = entry.get("prism:doi") or core.get("prism:doi") or ""
        abstract = core.get("dc:description") or ""
        cover_date = str(entry.get("prism:coverDate") or "")
        year = safe_int(cover_date[:4]) if cover_date[:4].isdigit() else None
        venue = entry.get("prism:publicationName") or core.get("prism:publicationName") or ""
        document_type = entry.get("subtypeDescription") or entry.get("prism:aggregationType") or ""
        citation_count = safe_int(entry.get("citedby-count") or core.get("citedby-count"))

        # Autores (vista META: coredata.dc:creator.author; si no, nodo authors/búsqueda)
        authors, authors_detail = _collect_authors(retrieval, core, entry)

        # PMID cruzado (Scopus lo expone cuando el registro está en PubMed)
        pmid = str(core.get("pubmed-id") or entry.get("pubmed-id") or "").strip()

        # Keywords de autor y áreas temáticas
        keywords: List[str] = []
        auth_keywords = (retrieval.get("authkeywords", {}) or {}).get("author-keyword")
        for kw in _as_list(auth_keywords):
            if isinstance(kw, dict):
                term = kw.get("$") or kw.get("author-keyword")
            else:
                term = kw
            if term:
                keywords.append(str(term).strip())
        subject_areas = []
        for area in _as_list((retrieval.get("subject-areas", {}) or {}).get("subject-area")):
            if isinstance(area, dict) and area.get("$"):
                subject_areas.append(area["$"])

        # Referencias
        references: List[str] = []
        for ref in _as_list((retrieval.get("references", {}) or {}).get("reference")):
            if isinstance(ref, dict):
                label = ref.get("ce:title") or ref.get("ce:source") or ref.get("@id")
                if label:
                    references.append(str(label))
        references_count = len(references) if references else None

        # Enlaces
        scopus_link = _link_href(entry, "scopus")
        full_text_link = _link_href(entry, "full-text")
        landing_page = scopus_link or (
            f"https://www.scopus.com/record/display.uri?eid={entry.get('eid', '')}&origin=resultslist"
            if entry.get("eid")
            else None
        )

        return CanonicalBuilder.build(
            source=self.source,
            source_id=sid or entry.get("eid", ""),
            doi=doi,
            title=title,
            abstract=abstract,
            authors=authors,
            authors_detail=authors_detail,
            keywords=keywords,
            year=year,
            venue=venue,
            document_type=document_type,
            language="",
            pmid=pmid,
            references_count=references_count,
            citation_count=citation_count,
            references=references,
            pdf_url=full_text_link,
            landing_page=landing_page,
            open_access=entry.get("openaccess"),
            source_url=scopus_link,
            extra={"subject_areas": subject_areas, "eid": entry.get("eid")},
        )
