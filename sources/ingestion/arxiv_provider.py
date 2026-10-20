#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
arxiv_provider.py
=================

Proveedor de arXiv (https://export.arxiv.org/api/query): repositorio abierto de
preprints. Es gratuito y no requiere API key. Se recomienda usar `email` en la
configuración para identificarse ante el servicio (cortesía de arXiv).

La API devuelve un feed Atom con namespaces. Cada <entry> es un preprint con
metadatos: id de arXiv, título, abstract, autores, fecha de publicación,
categorías y enlaces (landing page y PDF).

Campos extraídos: arXiv ID, título, abstract, autores, año, venue (arXiv),
tipo de documento (preprint), idioma (en), pdf_url, landing_page.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from sources.common.common import logger, writeLog
from sources.ingestion.base import CanonicalBuilder, SearchProvider

ARXIV_API = "https://export.arxiv.org/api/query"

NS_ATOM = "{http://www.w3.org/2005/Atom}"
NS_ARXIV = "{http://arxiv.org/schemas/atom}"


def _text(element: Optional[ET.Element]) -> str:
    """Concatena todo el texto de un elemento (incluye subetiquetas)."""
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _clean_arxiv_id(raw_id: str) -> str:
    """Extrae el ID limpio de arXiv de una URL o del campo id."""
    raw_id = (raw_id or "").strip()
    if not raw_id:
        return ""
    if "/abs/" in raw_id:
        raw_id = raw_id.split("/abs/")[-1]
    elif "/pdf/" in raw_id:
        raw_id = raw_id.split("/pdf/")[-1]
    return raw_id


class ArxivProvider(SearchProvider):
    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------
    def fetch(self, query: str, *, max_results: int = 500) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": self.config.get("sort_by", "relevance"),
            "sortOrder": self.config.get("sort_order", "descending"),
        }

        writeLog("info", logger, f"[arXiv] consultando API: {query[:140]}...")
        xml = self.client.get(ARXIV_API, params=params, accept_json=False)

        return {
            "query": query,
            "xml": xml,
        }

    # ------------------------------------------------------------------
    # normalize
    # ------------------------------------------------------------------
    def normalize(self, raw: Any) -> List[Dict[str, Any]]:
        xml = raw.get("xml", "") if isinstance(raw, dict) else ""
        if not xml:
            return []

        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            writeLog("warning", logger, f"[arXiv] XML inválido: {exc}")
            return []

        records: List[Dict[str, Any]] = []
        for entry in root.findall(f"{NS_ATOM}entry"):
            record = self._parse_entry(entry)
            if record and record.get("title"):
                records.append(record)

        writeLog("info", logger, f"[arXiv] normalizados {len(records)} registros")
        return records

    def _parse_entry(self, entry: ET.Element) -> Optional[Dict[str, Any]]:
        # ID de arXiv (puede venir como URL completa o con versión, ej: 2103.12345v1)
        id_elem = entry.find(f"{NS_ATOM}id")
        arxiv_url = _text(id_elem)
        arxiv_id = _clean_arxiv_id(arxiv_url)

        title = _text(entry.find(f"{NS_ATOM}title"))
        abstract = _text(entry.find(f"{NS_ATOM}summary"))
        published = _text(entry.find(f"{NS_ATOM}published"))

        year: Optional[int] = None
        if published:
            try:
                year = int(published[:4])
            except (ValueError, TypeError):
                year = None

        # Autores
        authors: List[str] = []
        authors_detail: List[Dict[str, Any]] = []
        for author in entry.findall(f"{NS_ATOM}author"):
            name = _text(author.find(f"{NS_ATOM}name"))
            if name and name not in authors:
                authors.append(name)
                authors_detail.append({"name": name})

        # Categorías (arxiv:primary_category y atom:category)
        categories: List[str] = []
        primary_cat = entry.find(f"{NS_ARXIV}primary_category")
        if primary_cat is not None and primary_cat.get("term"):
            categories.append(primary_cat.get("term"))
        for cat in entry.findall(f"{NS_ATOM}category"):
            term = cat.get("term")
            if term and term not in categories:
                categories.append(term)

        # Enlaces: landing page (alternate) y PDF
        landing_page = None
        pdf_url = None
        for link in entry.findall(f"{NS_ATOM}link"):
            rel = link.get("rel", "")
            href = link.get("href", "")
            if rel == "alternate" and href:
                landing_page = href
            elif ("pdf" in rel.lower()) or ("/pdf/" in href):
                if not pdf_url:
                    pdf_url = href

        # Fallbacks
        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        if not landing_page and arxiv_url:
            landing_page = arxiv_url

        updated = _text(entry.find(f"{NS_ATOM}updated"))

        return CanonicalBuilder.build(
            source=self.source,
            source_id=arxiv_id,
            doi="",
            title=title,
            abstract=abstract,
            authors=authors,
            authors_detail=authors_detail,
            keywords=categories,
            year=year,
            venue="arXiv",
            document_type="preprint",
            language="en",
            references_count=None,
            citation_count=None,
            pmid="",
            pmcid="",
            oa_url="",
            pdf_url=pdf_url,
            landing_page=landing_page,
            open_access="open",
            source_url=arxiv_url,
            extra={
                "arxiv_id": arxiv_id,
                "categories": categories,
                "published": published,
                "updated": updated,
            },
        )
