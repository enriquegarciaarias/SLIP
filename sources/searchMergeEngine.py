#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
searchMergeEngine.py
====================

Fusiona y deduplica los papers de todas las fuentes configuradas en
`config.json > search.availables` y produce `canonical.json`.

Estrategia de identidad (Fase 3)
--------------------------------
En lugar de una única clave (DOI o título), cada registro aporta varias
claves de identidad y los registros que comparten CUALQUIERA de ellas se
fusionan de forma transitiva (union-find). Prioridad de fiabilidad:

    1. PMID                 -> identidad unívoca en PubMed.
    2. DOI normalizado      -> identidad editorial (se normalizan prefijos).
    3. Título normalizado + año.
    4. Título normalizado   -> solo si es suficientemente largo y no hay año
                               (configurable: search.merge.by_title_only_min_chars).

Esto captura duplicados que la clave única perdía: p. ej. un registro de
PubMed con DOI erróneo y su gemelo de Scopus con DOI correcto se unen por
título+año; y variantes de DOI (`...2018.3.008` vs `...2018.03.008`) se unen
por título+año.

El `global_id` resultante conserva el formato histórico `doi:<doi>` /
`title:<sha1>` para no romper la bóveda de PDFs (`adquisitionEngine`,
`manualIngestion`). El PMID no se usa como `global_id`.

La fusión es NO destructiva: conserva y combina metadatos de todas las
fuentes (autores, keywords, referencias, años, enlaces, etc.).
"""

from sources.common.common import logger, processControl, writeLog
from sources.common.utils import normalized_title, normalize_doi, sha1, safe_int, inicioModulo

from collections import Counter, defaultdict
from pathlib import Path
import json


# ==========================================================
# Configuración
# ==========================================================

DEFAULT_TITLE_ONLY_MIN_CHARS = 25


def load_merge_config() -> int:
    """Lee `search.merge.by_title_only_min_chars` de config.json."""
    defaults = getattr(processControl, "defaults", None) or {}
    search_cfg = defaults.get("search", {}) if isinstance(defaults, dict) else {}
    merge_cfg = search_cfg.get("merge", {}) if isinstance(search_cfg, dict) else {}
    if not isinstance(merge_cfg, dict):
        merge_cfg = {}
    return int(merge_cfg.get("by_title_only_min_chars", DEFAULT_TITLE_ONLY_MIN_CHARS))


# ==========================================================
# Identidad
# ==========================================================

def build_global_id(paper: dict) -> str:
    """ID estable para la bóveda: doi:<doi> o title:<sha1> (formato histórico)."""
    doi = normalize_doi(paper.get("doi"))
    if doi:
        return f"doi:{doi}"
    title = normalized_title(paper.get("title", ""))
    return f"title:{sha1(title)}"


def _clean_str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def build_identity_keys(paper: dict, title_only_min_chars: int) -> set[str]:
    """Conjunto de claves de identidad de un registro (para el union-find)."""
    keys: set[str] = set()

    pmid = _clean_str(paper.get("pmid"))
    if pmid:
        keys.add(f"pmid:{pmid}")

    doi = normalize_doi(paper.get("doi"))
    if doi:
        keys.add(f"doi:{doi}")

    title = normalized_title(paper.get("title", ""))
    year = paper.get("year")
    if title and year:
        keys.add(f"ty:{sha1(title)}:{year}")
    elif title and len(title) >= title_only_min_chars:
        # Sin año: solo fusionamos por título si es lo bastante específico.
        keys.add(f"t:{sha1(title)}")

    return keys


# ==========================================================
# Union-Find
# ==========================================================

class _UnionFind:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_b] = root_a


# ==========================================================
# Normalización de un registro
# ==========================================================

def normalize_paper(paper: dict, source: str) -> dict:
    """Conserva TODOS los campos y homogeneiza los mínimos para fusionar."""
    return {
        "paper_id": paper.get("paper_id"),
        "source": source,
        "source_index": paper.get("source_index"),
        "source_id": paper.get("source_id"),
        "original_id": paper.get("original_id"),
        "doi": normalize_doi(paper.get("doi")),
        "title": paper.get("title", "") or "",
        "normalized_title": normalized_title(paper.get("title", "")),
        "abstract": paper.get("abstract", "") or "",
        "year": safe_int(paper.get("year")),
        "authors": paper.get("authors", []) or [],
        "authors_detail": paper.get("authors_detail", []) or [],
        "keywords": paper.get("keywords", []) or [],
        "venue": paper.get("venue", "") or "",
        "document_type": paper.get("document_type", "") or "",
        "language": paper.get("language", "") or "",
        "references_count": safe_int(paper.get("references_count")),
        "citation_count": safe_int(paper.get("citation_count")),
        "references": paper.get("references", []) or [],
        "references_meta": paper.get("references_meta", []) or [],
        "relevance_score": paper.get("relevance_score"),
        "selected": bool(paper.get("selected", False)),
        "has_full_text": bool(paper.get("has_full_text", False)),
        "pmid": _clean_str(paper.get("pmid")) or None,
        "pmcid": _clean_str(paper.get("pmcid")) or None,
        "ieee_terms": paper.get("ieee_terms", []) or [],
        "oa_url": paper.get("oa_url"),
        "open_access": paper.get("open_access"),
        "pdf_url": paper.get("pdf_url"),
        "pdf_source": paper.get("pdf_source"),
        "pdf_inventory": paper.get("pdf_inventory"),
        "landing_page": paper.get("landing_page"),
        "source_url": paper.get("source_url"),
        "subject_areas": paper.get("subject_areas", []) or [],
        "raw_source_file": paper.get("raw_source_file"),
        "snapshot_ref": paper.get("snapshot_ref"),
    }


# ==========================================================
# Fusión de un grupo
# ==========================================================

def _first_nonempty(items: list[dict], key: str):
    for item in items:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _longest(items: list[dict], key: str):
    values = [v for v in (item.get(key) for item in items) if isinstance(v, str) and v.strip()]
    return max(values, key=len) if values else None


def _most_common_year(items: list[dict]):
    years = [item.get("year") for item in items if item.get("year")]
    if not years:
        return None
    counts = Counter(years)
    max_count = max(counts.values())
    candidates = [y for y, c in counts.items() if c == max_count]
    return min(candidates)


def _union_list(items: list[dict], key: str) -> list:
    seen = set()
    merged = []
    for item in items:
        for value in item.get(key, []) or []:
            marker = value if isinstance(value, (str, int, float, tuple)) else json.dumps(value, sort_keys=True, default=str)
            if marker not in seen:
                seen.add(marker)
                merged.append(value)
    return merged


def _pick_primary(items: list[dict]) -> dict:
    """Registro más completo, con preferencia por tener DOI; determinista."""
    def score(item: dict) -> tuple:
        has_doi = 1 if item.get("doi") else 0
        filled = sum(
            1 for k in ("title", "abstract", "year", "venue", "document_type", "language")
            if item.get(k)
        )
        return (has_doi, filled, len(item.get("authors", []) or []), len(item.get("keywords", []) or []))
    return max(items, key=score)


def merge_group(items: list[dict]) -> dict:
    primary = _pick_primary(items)
    sources = sorted({item["source"] for item in items if item.get("source")})
    source_indices = {
        item["source"]: item.get("source_index")
        for item in items
        if item.get("source")
    }
    source_ids = {
        item["source"]: item.get("source_id")
        for item in items
        if item.get("source") and item.get("source_id")
    }

    # DOI "de consenso": el más frecuente; desempate lexicográfico.
    dois = [item["doi"] for item in items if item.get("doi")]
    chosen_doi = None
    if dois:
        chosen_doi = sorted(Counter(dois).items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    pmids = sorted({item["pmid"] for item in items if item.get("pmid")})
    merged_title = _longest(items, "title")

    # global_id alineado con el DOI elegido (o con el título fusionado).
    if chosen_doi:
        global_id = f"doi:{chosen_doi}"
    else:
        global_id = f"title:{sha1(normalized_title(merged_title or ''))}"

    merged = {
        "paper_id": primary.get("paper_id"),
        "global_id": global_id,
        "sources": sources,
        "source": primary.get("source"),
        "source_indices": source_indices,
        "source_ids": source_ids,
        "doi": chosen_doi,
        "dois": sorted(set(dois)),
        "pmid": pmids[0] if pmids else None,
        "pmids": pmids,
        "pmcid": _first_nonempty(items, "pmcid"),
        "title": merged_title,
        "normalized_title": normalized_title(merged_title or ""),
        "abstract": _longest(items, "abstract"),
        "year": _most_common_year(items),
        "authors": _union_list(items, "authors"),
        "authors_detail": _union_list(items, "authors_detail"),
        "keywords": sorted({k for item in items for k in (item.get("keywords") or []) if k}),
        "venue": _longest(items, "venue"),
        "document_type": _first_nonempty(items, "document_type"),
        "language": _first_nonempty(items, "language"),
        "references_count": max(
            (item.get("references_count") for item in items if item.get("references_count")),
            default=None,
        ),
        "citation_count": max(
            (item.get("citation_count") for item in items if item.get("citation_count")),
            default=None,
        ),
        "references": _union_list(items, "references"),
        "references_meta": _union_list(items, "references_meta"),
        "relevance_score": _first_nonempty(items, "relevance_score"),
        "selected": any(item.get("selected") for item in items),
        "has_full_text": any(item.get("has_full_text") for item in items),
        "ieee_terms": sorted({t for item in items for t in (item.get("ieee_terms") or []) if t}),
        "subject_areas": sorted({t for item in items for t in (item.get("subject_areas") or []) if t}),
        "oa_url": _first_nonempty(items, "oa_url"),
        "open_access": _first_nonempty(items, "open_access"),
        "pdf_url": _first_nonempty(items, "pdf_url"),
        "pdf_source": _first_nonempty(items, "pdf_source"),
        "pdf_inventory": _first_nonempty(items, "pdf_inventory"),
        "landing_page": _first_nonempty(items, "landing_page"),
        "source_url": _first_nonempty(items, "source_url"),
        "raw_source_file": _first_nonempty(items, "raw_source_file"),
        "snapshot_ref": _first_nonempty(items, "snapshot_ref"),
        "merged_paper_ids": [item.get("paper_id") for item in items if item.get("paper_id")],
        "merged_from": len(items),
    }
    return merged


# ==========================================================
# Fusión global
# ==========================================================

def merge_sources(sources_data: dict) -> list:
    """Fusiona y deduplica por claves de identidad múltiples (union-find)."""
    title_only_min = load_merge_config()

    records: list[dict] = []
    for source, papers in sources_data.items():
        for paper in papers or []:
            if not isinstance(paper, dict):
                continue
            record = normalize_paper(paper, source)
            record["_identity_keys"] = build_identity_keys(record, title_only_min)
            records.append(record)

    if not records:
        return []

    uf = _UnionFind(len(records))
    buckets: dict[str, int] = {}
    for index, record in enumerate(records):
        for key in record["_identity_keys"]:
            if key in buckets:
                uf.union(index, buckets[key])
            else:
                buckets[key] = index

    groups: dict[int, list[dict]] = defaultdict(list)
    for index, record in enumerate(records):
        groups[uf.find(index)].append(record)

    merged = [merge_group(items) for items in groups.values()]

    # Orden determinista: por número de fuentes (desc) y paper_id.
    merged.sort(key=lambda r: (-len(r.get("sources", [])), r.get("paper_id") or ""))
    return merged


# ==========================================================
# Carga de fuentes
# ==========================================================

def load_source_data(source_name: str, search_dir: Path) -> list:
    source_file = search_dir / f"{source_name}_search.json"
    if not source_file.exists():
        writeLog("warning", logger, f"[MERGE] {source_name}_search.json not found in {search_dir}")
        return []
    with open(source_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    writeLog("info", logger, f"[MERGE] Loaded {len(data)} papers from {source_name}")
    return data


# ==========================================================
# Entrada del módulo
# ==========================================================

def processSearchMergeEngine():
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

    total_raw = sum(len(v) for v in sources_data.values())
    total_unique = len(canonical_papers)
    merged_groups = [p for p in canonical_papers if p.get("merged_from", 1) > 1]

    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, "[MERGE] COMPLETE")
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, f"  Raw papers: {total_raw}")
    writeLog("info", logger, f"  Unique papers: {total_unique}")
    writeLog("info", logger, f"  Duplicates removed: {total_raw - total_unique}")
    writeLog("info", logger, f"  Merged groups (multi-source): {len(merged_groups)}")

    biggest = sorted(canonical_papers, key=lambda p: -p.get("merged_from", 1))[:3]
    for paper in biggest:
        if paper.get("merged_from", 1) > 1:
            writeLog("info", logger,
                     f"    [{paper.get('paper_id')}] merged {paper['merged_from']} records "
                     f"from {paper.get('sources')}")

    source_counts = defaultdict(int)
    for paper in canonical_papers:
        for source in paper.get("sources", []):
            source_counts[source] += 1
    for source, count in sorted(source_counts.items()):
        writeLog("info", logger, f"  {source.upper()}: {count} unique papers")

    writeLog("info", logger, f"  Output: {output_file}")

    return canonical_papers


if __name__ == "__main__":
    processSearchMergeEngine()
