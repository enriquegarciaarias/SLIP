#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
pubmed_provider.py
==================

Proveedor de PubMed basado en las E-utilities de NCBI (gratuitas).

Flujo:
  1. `esearch.fcgi`  -> lista de PMIDs para la query (JSON).
  2. `efetch.fcgi`   -> registros completos en XML (por lotes).

El snapshot guarda la respuesta de esearch, la lista de IDs y los lotes XML
crudos, de forma que `normalize()` puede re-ejecutarse sin red.

Campos extraídos: PMID, PMCID, DOI, título, abstract, autores, revista, año,
idioma, tipo de publicación, keywords (KeywordList + MeSH), referencias.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from sources.common.common import logger, writeLog
from sources.common.utils import safe_int
from sources.ingestion.base import CanonicalBuilder, SearchProvider

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _text(element: Optional[ET.Element]) -> str:
    """Concatena todo el texto de un elemento (incluye subetiquetas como <i>)."""
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _join_children(element: Optional[ET.Element], tag: str = "AbstractText") -> str:
    if element is None:
        return ""
    parts = []
    for child in element.findall(tag):
        label = child.get("Label")
        value = _text(child)
        if not value:
            continue
        parts.append(f"{label}: {value}" if label else value)
    return " ".join(parts)


class PubmedProvider(SearchProvider):
    def _common_params(self) -> Dict[str, Any]:
        params: Dict[str, Any] = {"db": "pubmed", "tool": self.config.get("tool", "SLIP")}
        if self.config.get("email"):
            params["email"] = self.config["email"]
        if self.config.get("api_key"):
            params["api_key"] = self.config["api_key"]
        return params

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------
    def fetch(self, query: str, *, max_results: int = 500) -> Dict[str, Any]:
        params = self._common_params()
        params.update(
            {
                "term": query,
                "retmax": max_results,
                "retmode": "json",
                "sort": "relevance",
            }
        )
        search = self.client.get(f"{EUTILS_BASE}/esearch.fcgi", params=params)
        id_list = search.get("esearchresult", {}).get("idlist", []) or []
        writeLog("info", logger, f"[PubMed] esearch devolvió {len(id_list)} PMIDs")

        batch_size = int(self.config.get("batch_size", 200))
        xml_batches: List[str] = []
        for start in range(0, len(id_list), batch_size):
            chunk = id_list[start:start + batch_size]
            fetch_params = self._common_params()
            fetch_params.update(
                {"id": ",".join(chunk), "retmode": "xml", "rettype": "abstract"}
            )
            xml = self.client.get(
                f"{EUTILS_BASE}/efetch.fcgi", params=fetch_params, accept_json=False
            )
            xml_batches.append(xml)
            writeLog("info", logger, f"[PubMed] efetch lote {start // batch_size + 1}: {len(chunk)} registros")

        return {
            "query": query,
            "esearch": search,
            "idlist": id_list,
            "xml_batches": xml_batches,
        }

    # ------------------------------------------------------------------
    # normalize
    # ------------------------------------------------------------------
    def normalize(self, raw: Any) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for xml in raw.get("xml_batches", []):
            if not xml:
                continue
            try:
                root = ET.fromstring(xml)
            except ET.ParseError as exc:
                writeLog("warning", logger, f"[PubMed] XML inválido: {exc}")
                continue

            for article in root.iter("PubmedArticle"):
                record = self._parse_article(article)
                if record and record.get("title"):
                    records.append(record)

        return records

    def _parse_article(self, article: ET.Element) -> Optional[Dict[str, Any]]:
        citation = article.find("MedlineCitation")
        if citation is None:
            return None
        art = citation.find("Article")
        if art is None:
            return None

        pmid = (citation.findtext("PMID") or "").strip()
        title = _text(art.find("ArticleTitle"))
        abstract = _join_children(art.find("Abstract"), "AbstractText")

        journal = art.find("Journal")
        venue = ""
        year: Optional[int] = None
        if journal is not None:
            venue = _text(journal.find("Title")) or _text(journal.find("ISOAbbreviation"))
            pub_date = journal.find("JournalIssue/PubDate")
            if pub_date is not None:
                year = safe_int(pub_date.findtext("Year"))
                if year is None:
                    medline_date = pub_date.findtext("MedlineDate") or ""
                    digits = "".join(ch for ch in medline_date if ch.isdigit())
                    if len(digits) >= 4:
                        year = safe_int(digits[:4])

        # Autores: solo AuthorList (no FAU/AU duplicados)
        authors: List[str] = []
        authors_detail: List[Dict[str, Any]] = []
        author_list = art.find("AuthorList")
        if author_list is not None:
            for author in author_list.findall("Author"):
                collective = author.findtext("CollectiveName")
                if collective:
                    name = collective.strip()
                    detail = {"name": name, "collective": True}
                else:
                    last = (author.findtext("LastName") or "").strip()
                    fore = (author.findtext("ForeName") or author.findtext("Initials") or "").strip()
                    name = f"{last}, {fore}".strip(", ") if last else fore
                    detail = {
                        "name": name,
                        "last_name": last,
                        "fore_name": fore,
                        "orcid": author.findtext("Identifier"),
                    }
                if name:
                    authors.append(name)
                    authors_detail.append(detail)

        # Tipos de publicación e idioma
        pub_types = [_text(pt) for pt in art.iter("PublicationType")]
        document_type = pub_types[0] if pub_types else "article"
        language = _text(art.find("Language"))

        # Keywords: KeywordList + MeSH
        keywords: List[str] = []
        keyword_list = citation.find("KeywordList")
        if keyword_list is not None:
            keywords.extend(k.strip() for k in (_text(kw) for kw in keyword_list.findall("Keyword")) if k)
        for descriptor in citation.iter("DescriptorName"):
            term = _text(descriptor)
            if term:
                keywords.append(term)

        # Identificadores
        ids: Dict[str, str] = {}
        for article_id in article.iter("ArticleId"):
            id_type = article_id.get("IdType")
            if id_type:
                ids[id_type] = (article_id.text or "").strip()
        doi = ids.get("doi", "")
        pmcid = ids.get("pmc", "")

        # Referencias
        references: List[str] = []
        for ref in article.iter("Reference"):
            citation_text = _text(ref.find("Citation"))
            if citation_text:
                references.append(citation_text)
        references_count = len(references) if references else None

        landing_page = None
        if doi:
            landing_page = f"https://doi.org/{doi}"
        elif pmid:
            landing_page = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

        oa_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/" if pmcid else ""

        return CanonicalBuilder.build(
            source=self.source,
            source_id=pmid,
            doi=doi,
            title=title,
            abstract=abstract,
            authors=authors,
            authors_detail=authors_detail,
            keywords=keywords,
            year=year,
            venue=venue,
            document_type=document_type,
            language=language,
            references_count=references_count,
            citation_count=None,
            pmid=pmid,
            pmcid=pmcid,
            oa_url=oa_url,
            references=references,
            landing_page=landing_page,
            source_url=landing_page,
            extra={"publication_types": pub_types},
        )
