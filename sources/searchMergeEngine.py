#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
processSearchMergeEngine.py

Input:
    output/{subject}/wos_search.json
    output/{subject}/scopus_search.json
    output/{subject}/ieee_search.json
    output/{subject}/pubmed_search.json

Output:
    output/{subject}/canonical.json

CHANGED: Conserva los paper_id originales (wos_1, scopus_42, ieee_7, pubmed_3)
en lugar de regenerarlos con build_paper_id().
"""

from sources.common.common import logger, processControl, writeLog
from sources.common.utils import normalized_title, sha1, inicioModulo

from collections import defaultdict
from pathlib import Path
import json


def build_global_id(paper):
    """
    Construye un ID global para deduplicación basado en DOI o título.
    Este ID NO se guarda como paper_id, solo se usa para agrupar duplicados.
    """
    doi = (paper.get("doi") or "").strip().lower()
    title = normalized_title(paper.get("title", ""))

    if doi:
        return f"doi:{doi}"

    return f"title:{sha1(title)}"


def normalize_paper(paper, source):
    """
    Normaliza un paper de cualquier fuente a un formato común.

    CHANGED: Conserva el paper_id original (wos_1, scopus_42, etc.)
    en lugar de regenerarlo.
    """
    return {
        "paper_id": paper.get("paper_id"),  # ← CONSERVAR ID ORIGINAL
        "source": source,
        "source_index": paper.get("source_index"),
        "doi": (paper.get("doi") or "").strip().lower(),
        "title": paper.get("title", ""),
        "normalized_title": normalized_title(paper.get("title", "")),
        "abstract": paper.get("abstract", ""),
        "year": paper.get("year"),
        "authors": paper.get("authors", []),
        "keywords": paper.get("keywords", []),
        "venue": paper.get("venue", ""),
        "citation_count": paper.get("citation_count"),
        "relevance_score": paper.get("relevance_score"),
        "selected": paper.get("selected", False),
        "has_full_text": paper.get("has_full_text", False),
        # Campos específicos (pueden venir de algunas fuentes)
        "pmid": paper.get("pmid"),
        "pmcid": paper.get("pmcid"),
        "ieee_terms": paper.get("ieee_terms", []),
        "open_access": paper.get("open_access"),
        "pdf_url": paper.get("pdf_url"),
        "landing_page": paper.get("landing_page")
    }


def merge_sources(sources_data: dict) -> list:
    """
    Fusiona múltiples fuentes de papers (WoS, Scopus, IEEE, PubMed, etc.)

    CHANGED: Usa global_id para deduplicación, pero conserva los paper_id
    originales de cada fuente.
    """
    all_records = []

    for source, papers in sources_data.items():
        for paper in papers:
            # Añadir global_id para deduplicación (no se guarda en el paper final)
            record = normalize_paper(paper, source)
            record["_global_id"] = build_global_id(paper)
            all_records.append(record)

    # Agrupar por global_id (no por paper_id)
    groups = defaultdict(list)
    for paper in all_records:
        groups[paper["_global_id"]].append(paper)

    # Fusionar grupos
    merged = {}
    for gid, items in groups.items():
        sources_list = sorted(set(i["source"] for i in items))
        source_indices = {
            i["source"]: i.get("source_index")
            for i in items
            if i.get("source_index") is not None
        }

        # Elegir el paper_id del primer elemento (todos representan el mismo paper)
        # Podría mejorarse seleccionando el más completo; por simplicidad se utiliza el primero
        primary_paper_id = items[0]["paper_id"]

        merged[gid] = {
            "paper_id": primary_paper_id,  # ← Conserva el ID original
            "global_id": gid,  # ← Añadir global_id para trazabilidad
            "sources": sources_list,
            "source_indices": source_indices,
            "doi": next((i["doi"] for i in items if i.get("doi")), None),
            "title": max((i["title"] for i in items if i.get("title")), key=len, default=None),
            "normalized_title": items[0]["normalized_title"],
            "abstract": max((i["abstract"] for i in items if i.get("abstract")), key=len, default=None),
            "year": max((i["year"] for i in items if i.get("year")), default=None),
            "authors": sorted(set(a for i in items for a in i.get("authors", []))),
            "keywords": sorted(set(k for i in items for k in i.get("keywords", []))),
            "venue": max((i["venue"] for i in items if i.get("venue")), key=len, default=None),
            "citation_count": max((i.get("citation_count") for i in items if i.get("citation_count")), default=None),
            "relevance_score": next((i.get("relevance_score") for i in items if i.get("relevance_score")), None),
            "selected": any(i.get("selected") for i in items),
            "has_full_text": any(i.get("has_full_text") for i in items),
            # Conservar campos específicos de la fuente más completa
            "pmid": next((i.get("pmid") for i in items if i.get("pmid")), None),
            "pmcid": next((i.get("pmcid") for i in items if i.get("pmcid")), None),
            "ieee_terms": list(set(t for i in items for t in i.get("ieee_terms", []))),
            "open_access": next((i.get("open_access") for i in items if i.get("open_access")), None),
            "pdf_url": next((i.get("pdf_url") for i in items if i.get("pdf_url")), None),
            "landing_page": next((i.get("landing_page") for i in items if i.get("landing_page")), None)
        }

    return list(merged.values())


def load_source_data(source_name: str, search_dir: Path) -> list:
    """
    Carga los datos de una fuente desde su archivo JSON.
    """
    source_file = search_dir / f"{source_name}_search.json"

    if not source_file.exists():
        writeLog("warning", logger, f"[MERGE] {source_name}_search.json not found in {search_dir}")
        return []

    with open(source_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    writeLog("info", logger, f"[MERGE] Loaded {len(data)} papers from {source_name}")
    return data


def processSearchMergeEngine():
    """
    Fusiona todas las fuentes definidas en processControl.defaults.get("search")["availables"]
    """
    input_dir, output_dir = inicioModulo("processSearchMergeEngine")

    search_config = processControl.defaults.get("search", {})
    availables = search_config.get("availables", [])

    if not availables:
        writeLog("warning", logger, "[MERGE] No search sources configured in 'availables'")
        return []

    writeLog("info", logger, f"[MERGE] Processing sources: {availables}")

    sources_data = {}
    for source in availables:
        data = load_source_data(source, output_dir)
        if data:
            sources_data[source] = data

    if not sources_data:
        writeLog("error", logger, "[MERGE] No data loaded from any source")
        return []

    canonical_papers = merge_sources(sources_data)

    output_file = output_dir / "canonical.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(canonical_papers, f, indent=2, ensure_ascii=False)

    total_unique = len(canonical_papers)
    total_raw = sum(len(v) for v in sources_data.values())

    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, "[MERGE] COMPLETE")
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, f"  Raw papers: {total_raw}")
    writeLog("info", logger, f"  Unique papers: {total_unique}")
    writeLog("info", logger, f"  Duplicates removed: {total_raw - total_unique}")

    source_counts = defaultdict(int)
    for paper in canonical_papers:
        for source in paper["sources"]:
            source_counts[source] += 1

    for source, count in sorted(source_counts.items()):
        writeLog("info", logger, f"  {source.upper()}: {count} papers")

    writeLog("info", logger, f"  Output: {output_file}")

    return canonical_papers


if __name__ == "__main__":
    processSearchMergeEngine()