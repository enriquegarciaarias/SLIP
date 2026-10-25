#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
completeSelection.py
====================

Selección completa de papers y generación del informe PRISMA.

Este módulo reemplaza a `copySelectedPapers.py`. Además de copiar los PDFs
de los papers seleccionados a `results/output/<subject>/selected_papers/`,
genera un informe PRISMA exhaustivo en `prisma.md` con:

1. Registros identificados por estrategia de búsqueda y fuente.
2. Deduplicación (canonical.json): únicos vs duplicados eliminados.
3. Screening (ranked_papers.json): papers tras ranking, excluidos por
   texto insuficiente, umbral de similitud aplicado.
4. Los 30 primeros papers que NO pasan el umbral de similitud, con su
   nivel de similitud (relevance_score).
5. Selección manual (selected_papers.json).
6. Adquisición de PDFs (selected_acquired.json, missing_pdfs.json).
7. Papers finales incluidos en el corpus.

Uso dentro del pipeline (main.py):
    from sources.completeSelection import processCompleteSelection
    processCompleteSelection()
"""

from __future__ import annotations

import json
import os
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from sources.common.common import logger, writeLog, processControl
from sources.common.utils import inicioModulo


# ---------------------------------------------------------------------------
# Umbral de similitud para considerar que un paper "pasa" el screening.
# Puede configurarse en config.json > defaults.ranking.similarity_threshold.
# Si no está definido, se usará 0.00 (sin filtro de similitud).
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> list[dict] | dict | None:
    """Carga un JSON de forma segura, devolviendo None si no existe o falla."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        writeLog("warning", logger,
                 f"[CompleteSelection] No se pudo leer {path}: {exc}")
        return None


def _load_paper_blacklist(output_dir: Path) -> dict[str, dict]:
    """
    Consolida todas las fuentes de paper_blacklist en un único dict:
      paper_id -> {"reason": str, "detail": str, "source": str}

    Fuentes (en orden de prioridad):
      1. config.json > defaults.paper_blacklist.entries
      2. rejected_papers.json (rechazos del operador durante selección interactiva)
      3. low_text_papers.json (papers con <500 palabras tras limpieza en corpusCleaning)
    """
    blacklist: dict[str, dict] = {}

    # 1) Desde config.json
    defaults = getattr(processControl, "defaults", None) or {}
    paper_bl_cfg = defaults.get("paper_blacklist", {}) if isinstance(defaults, dict) else {}
    for entry in paper_bl_cfg.get("entries", []) or []:
        pid = entry.get("id")
        if pid:
            blacklist[pid] = {
                "reason": entry.get("reason", "unknown"),
                "detail": entry.get("detail", ""),
                "source": "config.json",
            }

    # 2) Desde rejected_papers.json (operador dijo "N")
    rejected_file = output_dir / "rejected_papers.json"
    if rejected_file.exists():
        try:
            with open(rejected_file, "r", encoding="utf-8") as f:
                rejected = json.load(f)
            if isinstance(rejected, list):
                for entry in rejected:
                    pid = entry.get("paper_id")
                    if pid:
                        blacklist[pid] = {
                            "reason": entry.get("reason", "operator_rejected"),
                            "detail": entry.get("detail", ""),
                            "source": "operator_rejection",
                        }
        except Exception as exc:
            writeLog("warning", logger,
                     f"[CompleteSelection] No se pudo leer rejected_papers.json: {exc}")

    # 3) Desde low_text_papers.json (bajo contenido de texto)
    low_text_file = output_dir / "low_text_papers.json"
    if low_text_file.exists():
        try:
            with open(low_text_file, "r", encoding="utf-8") as f:
                low_text = json.load(f)
            if isinstance(low_text, list):
                for entry in low_text:
                    pid = entry.get("paper_id")
                    if pid:
                        blacklist[pid] = {
                            "reason": entry.get("reason", "low_text"),
                            "detail": entry.get("detail", ""),
                            "source": "corpusCleaning",
                        }
        except Exception as exc:
            writeLog("warning", logger,
                     f"[CompleteSelection] No se pudo leer low_text_papers.json: {exc}")

    return blacklist


def _count_by_strategy_and_source(search_dir: Path, availables: list[str]) -> dict:
    """
    Cuenta registros por fuente y estrategia de búsqueda leyendo los archivos
    *_search_NN.json de output_dir (solo estrategias numeradas, sin "legacy").

    Retorna un dict con estructura:
        {
            "by_strategy": {"01": {"scopus": N, "wos": N, ...}, "02": {...}},
            "by_source": {"scopus": N, "wos": N, ...},
            "total_raw": N,
            "files_detail": [{"file": "scopus_search_01.json", "source": "scopus", "strategy": "01", "count": N}, ...]
        }
    """
    import glob

    by_strategy: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_source: dict[str, int] = defaultdict(int)
    files_detail: list[dict] = []
    total_raw = 0

    # Solo archivos con sufijo de estrategia (ej: scopus_search_01.json)
    for source in availables:
        pattern = str(search_dir / f"{source}_search_*.json")
        for sf in sorted(glob.glob(pattern)):
            sf_path = Path(sf)
            data = _load_json(sf_path)
            if isinstance(data, list) and data:
                # Extraer el número de estrategia del nombre de archivo
                # Ej: scopus_search_01.json -> "01"
                name = sf_path.stem  # scopus_search_01
                parts = name.rsplit("_", 1)
                strategy_id = parts[-1] if len(parts) > 1 else "unknown"
                n = len(data)
                by_source[source] += n
                by_strategy[strategy_id][source] += n
                files_detail.append({
                    "file": sf_path.name,
                    "source": source,
                    "strategy": strategy_id,
                    "count": n,
                })
                total_raw += n

    return {
        "by_strategy": {k: dict(v) for k, v in by_strategy.items()},
        "by_source": dict(by_source),
        "total_raw": total_raw,
        "files_detail": files_detail,
    }


# ---------------------------------------------------------------------------
# Venues / conferencias objetivo para cuantificación en el informe PRISMA.
# Se leen desde results/input/<subject>/conceptsQuery.json (clave
# "prisma_target_venues"). Si no se encuentra, se usa una lista por defecto
# vacía para evitar fallar; el investigador debe configurarlas por subject.
# ---------------------------------------------------------------------------
DEFAULT_PRISMA_TARGET_VENUES: list[str] = []


def _load_target_venues(input_dir: Path) -> tuple[list[str], dict[str, list[str]]]:
    """
    Carga las venues objetivo y sus aliases desde conceptsQuery.json.

    Devuelve una tupla (venues, aliases) donde:
      - venues: lista de códigos de venue objetivo (ej. ["ACL", "EMNLP", ...])
      - aliases: diccionario {codigo_venue: [lista de nombres alternativos]}

    Si no existe conceptsQuery.json o no tiene las claves, devuelve listas vacías.
    """
    concepts_file = input_dir / "conceptsQuery.json"
    venues: list[str] = list(DEFAULT_PRISMA_TARGET_VENUES)
    aliases: dict[str, list[str]] = {}

    if not concepts_file.exists():
        writeLog("debug", logger,
                 f"[CompleteSelection] No se encontró {concepts_file}; usando venues por defecto")
        return venues, aliases

    try:
        with open(concepts_file, encoding="utf-8") as f:
            concepts_data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        writeLog("warning", logger,
                 f"[CompleteSelection] No se pudo leer {concepts_file}: {exc}")
        return venues, aliases

    venues_from_cfg = concepts_data.get("prisma_target_venues", [])
    if isinstance(venues_from_cfg, list):
        venues = [str(v).strip() for v in venues_from_cfg if str(v).strip()]

    aliases_from_cfg = concepts_data.get("prisma_venue_aliases", {})
    if isinstance(aliases_from_cfg, dict):
        for k, v in aliases_from_cfg.items():
            if isinstance(v, list):
                aliases[str(k).strip()] = [str(a).strip() for a in v if str(a).strip()]

    writeLog("info", logger,
             f"[CompleteSelection] Venues objetivo cargadas desde conceptsQuery.json: "
             f"{len(venues)} venues, {sum(len(a) for a in aliases.values())} aliases")

    return venues, aliases


def _venue_matches_target(venue_lower: str, target: str, aliases: list[str],
                          venue_from_doi_lower: str = "") -> bool:
    """
    Determina si un venue (ya en minúsculas) coincide con un venue objetivo.

    El matching se hace por:
      1. Subcadena exacta del código en el venue (ej. "acl" in "proceedings of acl 2024")
      2. Subcadena de cualquier alias en el venue (ej. "association for computational linguistics" in venue)
      3. Subcadena exacta del código en venue_from_doi (patrón del DOI, ej. "acl-long" → "acl")
      4. Coincidencia exacta del venue_from_doi con el target
    """
    if target.lower() in venue_lower:
        return True
    for alias in aliases:
        if alias and alias.lower() in venue_lower:
            return True
    # Matching contra venue_from_doi (patrón del DOI)
    if venue_from_doi_lower:
        if target.lower() in venue_from_doi_lower:
            return True
        if venue_from_doi_lower == target.lower():
            return True
    return False


def _count_venues_by_papers(
    papers: list[dict],
    venues: list[str],
    aliases: dict[str, list[str]],
) -> dict[str, int]:
    """
    Cuenta cuántos papers pertenecen a cada venue objetivo.
    El matching es case-insensitive y usa subcadenas del código, de sus aliases
    o del venue extraído del DOI (venue_from_doi).
    Un paper puede contarse en múltiples venues si su venue menciona varios.
    """
    counts: dict[str, int] = {v: 0 for v in venues}
    for p in papers or []:
        venue = str(p.get("venue") or "").lower()
        venue_from_doi = str(p.get("venue_from_doi") or "").lower()
        if not venue and not venue_from_doi:
            continue
        for target in venues:
            if _venue_matches_target(venue, target, aliases.get(target, []), venue_from_doi):
                counts[target] += 1
    return counts


def _build_prisma_md(output_dir: Path, input_dir: Path,
                     search_config: dict,
                     availables: list[str],
                     selected_count: int,
                     copied_count: int,
                     not_found_count: int,
                     ranked_papers: list[dict] | None = None,
                     ranking_outside_top_k: list[dict] | None = None,
                     paper_blacklist: dict[str, dict] | None = None,
                     target_venues: list[str] | None = None,
                     venue_aliases: dict[str, list[str]] | None = None) -> str:
    """
    Construye el contenido del archivo prisma.md con todas las estadísticas
    del proceso de selección PRISMA.
    """
    lines: list[str] = []

    # ------------------------------------------------------------------
    # Cargar venues objetivo desde conceptsQuery.json (si no se pasaron)
    # ------------------------------------------------------------------
    if target_venues is None:
        target_venues, venue_aliases = _load_target_venues(input_dir)
    elif venue_aliases is None:
        _, venue_aliases = _load_target_venues(input_dir)

    # ------------------------------------------------------------------
    # Cabecera
    # ------------------------------------------------------------------
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append("# PRISMA — Flujo de Selección de Papers")
    lines.append("")
    lines.append(f"**Fecha y hora de ejecución:** {now}")
    lines.append("")

    # ------------------------------------------------------------------
    # 1. Identificación: registros por estrategia y fuente
    # ------------------------------------------------------------------
    lines.append("## 1. Identificación — Registros por estrategia de búsqueda")
    lines.append("")

    stats = _count_by_strategy_and_source(output_dir, availables)

    lines.append("### 1.1 Registros por estrategia y fuente")
    lines.append("")
    lines.append("| Estrategia | Fuente | Registros |")
    lines.append("|------------|--------|-----------|")

    strategies = sorted(stats["by_strategy"].keys())
    for strat in strategies:
        sources = stats["by_strategy"][strat]
        strat_total = sum(sources.values())
        for source in sorted(sources.keys()):
            lines.append(f"| {strat} | {source.upper()} | {sources[source]} |")
        lines.append(f"| **{strat} (subtotal)** |  | **{strat_total}** |")

    lines.append("")
    lines.append(f"**Total registros identificados (brutos):** {stats['total_raw']}")
    lines.append("")

    lines.append("### 1.2 Registros por fuente (agregado todas las estrategias)")
    lines.append("")
    lines.append("| Fuente | Registros |")
    lines.append("|--------|-----------|")
    for source in sorted(stats["by_source"].keys()):
        lines.append(f"| {source.upper()} | {stats['by_source'][source]} |")
    lines.append("")

    lines.append("### 1.3 Detalle de archivos de búsqueda leídos")
    lines.append("")
    lines.append("| Archivo | Fuente | Estrategia | Registros |")
    lines.append("|---------|--------|------------|-----------|")
    for fd in stats["files_detail"]:
        lines.append(f"| {fd['file']} | {fd['source']} | {fd['strategy']} | {fd['count']} |")
    lines.append("")

    # ------------------------------------------------------------------
    # 2. Deduplicación
    # ------------------------------------------------------------------
    canonical = _load_json(output_dir / "canonical.json")
    canonical_count = len(canonical) if isinstance(canonical, list) else 0

    lines.append("## 2. Deduplicación — Fusión de fuentes")
    lines.append("")
    lines.append(f"- Registros brutos totales: {stats['total_raw']}")
    lines.append(f"- Papers únicos tras fusión (canonical.json): {canonical_count}")
    lines.append(f"- Duplicados eliminados: {stats['total_raw'] - canonical_count}")
    lines.append("")

    # Contar grupos mergeados (multi-fuente)
    merged_groups = 0
    if isinstance(canonical, list):
        merged_groups = sum(1 for p in canonical if p.get("merged_from", 1) > 1)
    lines.append(f"- Grupos fusionados (múltiples fuentes): {merged_groups}")
    lines.append("")

    # ------------------------------------------------------------------
    # 3. Screening — Ranking y umbral de similitud
    # ------------------------------------------------------------------
    ranked = _load_json(output_dir / "ranked_papers.json")
    ranked_list = ranked if isinstance(ranked, list) else []
    ranked_count = len(ranked_list)

    # Papers que NO pasaron el ranking (quedaron fuera del top_k)
    not_ranked_count = max(0, canonical_count - ranked_count)

    # Umbral de similitud: se aplica ANTES del top_k.
    # Debe venir obligatoriamente de config.json > defaults.ranking.similarity_threshold.
    # Si no existe, se lanza warning y se usa 0.00 (sin filtro de similitud).
    defaults = getattr(processControl, "defaults", None) or {}
    ranking_cfg = defaults.get("ranking", {}) if isinstance(defaults, dict) else {}
    if "similarity_threshold" not in ranking_cfg:
        writeLog("warning", logger,
                 "[CompleteSelection] 'similarity_threshold' no está definido en "
                 "config.json > defaults.ranking. Se usará 0.00 (sin filtro de similitud).")
        similarity_threshold = 0.0
    else:
        similarity_threshold = float(ranking_cfg["similarity_threshold"])
        writeLog("info", logger,
                 f"[CompleteSelection] Umbral de similitud: {similarity_threshold:.3f}")

    # ---- Criterio de comparación: string base del estudio ----
    # Se construye igual que en rankingEngine.build_query_from_study_description:
    # objectives + keywords (x2) + focus_terms (x3) de studyDescription.json
    # Para el reporte se muestran los elementos únicos, uno por línea,
    # sin las repeticiones por peso que usa el ranking.
    subject = getattr(processControl.args, "subject", "sensores")
    # input_dir ya viene resuelto por inicioModulo (respeta results_root si está definido)
    input_subject_dir = input_dir

    study_desc_path = input_subject_dir / "studyDescription.json"
    study_objectives = []
    study_keywords = []
    study_focus_terms = []
    try:
        if study_desc_path.exists():
            with open(study_desc_path, "r", encoding="utf-8") as f:
                study_desc = json.load(f)
            project = study_desc.get("project", {})
            study_objectives = project.get("objectives", [])
            study_keywords = project.get("keywords", [])
            study_focus_terms = project.get("focus_terms", [])
        else:
            writeLog("warning", logger,
                     f"[CompleteSelection] No se encontró {study_desc_path}")
    except Exception as exc:  # noqa: BLE001
        writeLog("error", logger,
                 f"[CompleteSelection] Error leyendo studyDescription.json: {exc}")

    # Construir string para el ranking (con repeticiones por peso, como en rankingEngine)
    study_query_parts = [
        " ".join(study_objectives),
        " ".join(study_keywords), " ".join(study_keywords),
        " ".join(study_focus_terms), " ".join(study_focus_terms), " ".join(study_focus_terms),
    ]
    study_query_string = " ".join(p for p in study_query_parts if p).strip()

    # Las estrategias de búsqueda de conceptsQuery.json (para trazabilidad)
    concepts_query_strategies = {}
    cq_path = input_subject_dir / "conceptsQuery.json"
    try:
        if cq_path.exists():
            with open(cq_path, "r", encoding="utf-8") as f:
                cq = json.load(f)
            qs = cq.get("query_strategies", {})
            if isinstance(qs, dict):
                for sid, scfg in qs.items():
                    concepts_query_strategies[sid] = {
                        "query_generica": scfg.get("query", ""),
                        "queries_por_fuente": scfg.get("queries", {}),
                    }
        else:
            writeLog("warning", logger,
                     f"[CompleteSelection] No se encontró {cq_path}")
    except Exception as exc:  # noqa: BLE001
        writeLog("error", logger,
                 f"[CompleteSelection] Error leyendo conceptsQuery.json: {exc}")

    # De los papers que pasaron el ranking, cuántos pasan/no pasan el umbral de similitud
    # El mismo campo relevance_score (cosine similarity) se usa tanto para
    # establecer el top_k en el ranking como para aplicar este umbral en el reporte.
    passing = [p for p in ranked_list
               if isinstance(p.get("relevance_score"), (int, float))
               and p["relevance_score"] >= similarity_threshold]
    failing = [p for p in ranked_list
               if isinstance(p.get("relevance_score"), (int, float))
               and p["relevance_score"] < similarity_threshold]

    # Los 20 papers con MÁS coseno que NO superan el umbral
    # (orden descendente por score: los mejores de los que fallan)
    failing_sorted_desc = sorted(failing, key=lambda p: p.get("relevance_score", 0),
                                 reverse=True)
    failing_top20 = failing_sorted_desc[:20]

    lines.append("## 3. Screening — Ranking y umbral de similitud")
    lines.append("")
    lines.append("### 3.1 Criterio de comparación (string base del estudio)")
    lines.append("")
    lines.append("El coseno de similitud se calcula entre este string de consulta")
    lines.append("y el contenido (título + abstract + keywords) de cada paper.")
    lines.append("")
    lines.append("**String de consulta del estudio (desde `studyDescription.json` > `project`):**")
    lines.append("")
    lines.append("El coseno de similitud se calcula comparando este string contra el contenido")
    lines.append("(título + abstract + keywords) de cada paper. Se muestran los elementos")
    lines.append("únicos por sección (sin las repeticiones por peso que usa el ranking).")
    lines.append("")
    lines.append("**Objetivos:**")
    if study_objectives:
        for obj in study_objectives:
            lines.append(f"  - {obj}")
    else:
        lines.append("  (no definido)")
    lines.append("")
    lines.append("**Keywords:**")
    if study_keywords:
        for kw in study_keywords:
            lines.append(f"  - {kw}")
    else:
        lines.append("  (no definido)")
    lines.append("")
    lines.append("**Términos de enfoque (focus_terms):**")
    if study_focus_terms:
        for ft in study_focus_terms:
            lines.append(f"  - {ft}")
    else:
        lines.append("  (no definido)")
    lines.append("")
    lines.append("> Nota: para el cálculo del ranking, `keywords` se repite 2 veces y `focus_terms` 3")
    lines.append("> veces para darles más peso en la similitud de coseno.")
    lines.append("")
    lines.append("**Estrategias de búsqueda (desde `conceptsQuery.json` > `query_strategies`):**")
    lines.append("")
    if concepts_query_strategies:
        for sid, scfg in concepts_query_strategies.items():
            lines.append(f"- **Estrategia {sid}:**")
            qg = scfg.get("query_generica", "")
            if qg:
                lines.append(f"  - Query genérica: {qg[:120]}{'...' if len(qg) > 120 else ''}")
            qpf = scfg.get("queries_por_fuente", {})
            if qpf:
                for fuente, qf in qpf.items():
                    lines.append(f"  - Query `{fuente}`: {qf[:120]}{'...' if len(qf) > 120 else ''}")
    else:
        lines.append("*No definidas.*")
    lines.append("")

    # ------------------------------------------------------------------
    # 3.2 Resultado del ranking y umbral de similitud (umbral ANTES del top_k)
    # ------------------------------------------------------------------
    # Leer estadísticas de ranking generadas por rankingEngine
    ranking_stats = _load_json(output_dir / "ranking_stats.json") or {}
    ranking_outside = _load_json(output_dir / "ranking_outside_top_k.json") or []

    passed_threshold = ranking_stats.get("passed_threshold", 0)
    outside_top_k_count = ranking_stats.get("outside_top_k", 0)
    top_k_final = ranking_stats.get("top_k_final", 0)
    similarity_threshold = ranking_stats.get("similarity_threshold", similarity_threshold)

    lines.append("### 3.2 Resultado del ranking y umbral de similitud")
    lines.append("")
    lines.append("**Flujo:** primero se aplica el umbral de similitud, luego el corte top_k.")
    lines.append("")
    lines.append(f"- Papers únicos tras deduplicación (canonical.json): {canonical_count}")
    lines.append(f"- Papers que SUPERAN el umbral de similitud (>= {similarity_threshold:.3f}): {passed_threshold}")
    lines.append(f"- Papers dentro del top_k (ranked_papers.json): {top_k_final}")
    lines.append(f"- Papers que superan el umbral pero se QUEDAN FUERA por top_k: {outside_top_k_count}")
    lines.append("")
    lines.append("**Nota:** el mismo valor de similitud de coseno (`relevance_score`) se usa")
    lines.append("tanto para aplicar el umbral como para ordenar y establecer el top_k.")
    lines.append("")

    # ------------------------------------------------------------------
    # 3.3 Calidad de metadatos en canonical (papers sin título/abstract/keywords completos)
    # ------------------------------------------------------------------
    def _field_complete(paper: dict, field: str) -> bool:
        val = paper.get(field)
        if field == "keywords":
            return isinstance(val, list) and len(val) > 0
        if isinstance(val, str):
            return len(val.strip()) > 0
        return False

    def _title_complete(paper: dict) -> bool:
        t = paper.get("title")
        return isinstance(t, str) and len(t.strip()) >= 5

    def _abstract_complete(paper: dict) -> bool:
        a = paper.get("abstract")
        return isinstance(a, str) and len(a.strip()) >= 50

    no_title_by_source: dict[str, int] = defaultdict(int)
    no_abstract_by_source: dict[str, int] = defaultdict(int)
    no_keywords_by_source: dict[str, int] = defaultdict(int)
    total_no_title = 0
    total_no_abstract = 0
    total_no_keywords = 0

    if isinstance(canonical, list):
        for p in canonical:
            sources = p.get("sources") or [p.get("source") or "?"]
            strategies = p.get("query_strategies") or []
            # Crear etiquetas fuente_estrategia (ej: scopus_01, ieee_02)
            source_strategy_labels = []
            for s in sources:
                if strategies:
                    for st in strategies:
                        source_strategy_labels.append(f"{s}_{st}")
                else:
                    source_strategy_labels.append(s)
            if not source_strategy_labels:
                source_strategy_labels = ["?"]

            if not _title_complete(p):
                total_no_title += 1
                for lbl in source_strategy_labels:
                    no_title_by_source[lbl] += 1
            if not _abstract_complete(p):
                total_no_abstract += 1
                for lbl in source_strategy_labels:
                    no_abstract_by_source[lbl] += 1
            if not _field_complete(p, "keywords"):
                total_no_keywords += 1
                for lbl in source_strategy_labels:
                    no_keywords_by_source[lbl] += 1

    lines.append("### 3.3 Calidad de metadatos en canonical (papers sin campos completos)")
    lines.append("")
    lines.append(f"- Papers sin título completo (total): {total_no_title}")
    if no_title_by_source:
        lines.append("  - Por fuente: " + ", ".join(
            f"{s}: {n}" for s, n in sorted(no_title_by_source.items())))
    lines.append(f"- Papers sin abstract completo (total): {total_no_abstract}")
    if no_abstract_by_source:
        lines.append("  - Por fuente: " + ", ".join(
            f"{s}: {n}" for s, n in sorted(no_abstract_by_source.items())))
    lines.append(f"- Papers sin keywords (total): {total_no_keywords}")
    if no_keywords_by_source:
        lines.append("  - Por fuente: " + ", ".join(
            f"{s}: {n}" for s, n in sorted(no_keywords_by_source.items())))
    lines.append("")

    # ------------------------------------------------------------------
    # 20 primeros con MÁS coseno que se quedaron fuera por top_k
    # ------------------------------------------------------------------
    lines.append("### 3.4 Los 20 papers con MÁS similitud de coseno que se quedan FUERA por top_k")
    lines.append("")
    lines.append("*Estos papers superaron el umbral de similitud pero no entran en el "
                 "top_k por el límite de cuota.*")
    lines.append("")
    if ranking_outside:
        # Ordenar por coseno descendente (los mejores de los que se quedan fuera)
        outside_sorted = sorted(ranking_outside,
                                key=lambda p: p.get("relevance_score", 0),
                                reverse=True)
        outside_top20 = outside_sorted[:20]
        lines.append("| # | Paper ID | Coseno | Título | Abstract (truncado) | Fuente |")
        lines.append("|---|----------|--------|--------|---------------------|--------|")
        for i, p in enumerate(outside_top20, 1):
            title = (p.get("title") or "")[:60]
            abstract = (p.get("abstract") or "")[:150]
            score = p.get("relevance_score", 0)
            pid = p.get("paper_id", "?")
            source = p.get("source", "?")
            lines.append(f"| {i} | {pid} | {score:.4f} | {title} | {abstract} | {source} |")
    else:
        lines.append("*No hay papers que se queden fuera por top_k.*")
    lines.append("")

    # ------------------------------------------------------------------
    # 4. Selección manual
    # ------------------------------------------------------------------
    selected = _load_json(output_dir / "selected_papers.json")
    selected_list = selected if isinstance(selected, list) else []

    lines.append("## 4. Selección manual de papers")
    lines.append("")
    lines.append(f"- Papers en selected_papers.json: {len(selected_list)}")
    lines.append(f"- Papers seleccionados (aceptados tras review): {selected_count}")
    lines.append("")

    # ------------------------------------------------------------------
    # 5. Adquisición de PDFs
    # ------------------------------------------------------------------
    acquired = _load_json(output_dir / "selected_acquired.json")
    acquired_list = acquired if isinstance(acquired, list) else []

    missing = _load_json(output_dir / "missing_pdfs.json")
    missing_list = missing if isinstance(missing, list) else []

    pdf_log = _load_json(output_dir / "pdf_download_log.json")

    lines.append("## 5. Adquisición de PDFs")
    lines.append("")
    lines.append(f"- Papers con PDF adquirido (selected_acquired.json): {len(acquired_list)}")
    lines.append(f"- PDFs copiados a selected_papers/: {copied_count}")
    lines.append(f"- PDFs no encontrados al copiar: {not_found_count}")
    lines.append(f"- Papers sin PDF descargable (missing_pdfs.json): {len(missing_list)}")
    lines.append("")

    if missing_list:
        lines.append("### 5.1 Papers con PDF no descargable (primeros 20)")
        lines.append("")
        lines.append("| Paper ID | Título | Fuente |")
        lines.append("|----------|--------|--------|")
        for p in missing_list[:20]:
            title = (p.get("title") or p.get("paper_id") or "?")[:70]
            pid = p.get("paper_id", "?")
            source = p.get("source", "?")
            lines.append(f"| {pid} | {title} | {source} |")
        lines.append("")

    # ------------------------------------------------------------------
    # 6. Inclusión final
    # ------------------------------------------------------------------
    lines.append("## 6. Inclusión final en el corpus")
    lines.append("")
    lines.append(f"- Papers seleccionados para el corpus: {selected_count}")
    lines.append(f"- Papers con PDF disponible para análisis completo: {copied_count}")
    lines.append(f"- Papers excluidos tras adquisición (sin PDF): {len(missing_list)}")
    lines.append("")

    # ------------------------------------------------------------------
    # 6.5. Distribución por venue / conferencia objetivo
    # ------------------------------------------------------------------
    # Se cuenta sobre los papers que SUPERAN el umbral de similitud
    # (ranked_papers.json = dentro de top_k + ranking_outside_top_k.json
    # = fuera de top_k pero > umbral), no solo sobre los seleccionados
    # manualmente por el investigador.
    ranked_papers = ranked_papers or []
    ranking_outside_top_k = ranking_outside_top_k or []

    # Papers que SUPERAN el umbral (total = top_k + outside_top_k)
    papers_above_threshold = list(ranked_papers) + list(ranking_outside_top_k)

    venue_counts_threshold = _count_venues_by_papers(papers_above_threshold, target_venues, venue_aliases)
    venue_counts_top_k = _count_venues_by_papers(ranked_papers, target_venues, venue_aliases)
    total_target_threshold = sum(venue_counts_threshold.values())
    total_target_top_k = sum(venue_counts_top_k.values())

    lines.append("## 6.5. Distribución de papers por venue / conferencia objetivo")
    lines.append("")
    lines.append(
        "Conteo sobre papers que SUPERAN el umbral de similitud de coseno "
        "(umbral aplicado ANTES del top_k). Se distingue entre:"
    )
    lines.append("")
    lines.append(
        f"- Papers que superan el umbral (total): {len(papers_above_threshold)} "
        f"(estos están dentro del top_k o se quedan fuera solo por el cupo)"
    )
    lines.append(
        f"- Papers dentro del top_k (ranked_papers.json): {len(ranked_papers)}"
    )
    lines.append("")
    lines.append(
        f"De los {total_target_threshold} papers con venue objetivo entre los que "
        f"superan el umbral, {total_target_top_k} están dentro del top_k."
    )
    lines.append("")
    lines.append("| Venue / Conferencia | Papers > umbral | Papers en top_k |")
    lines.append("|---------------------|-----------------|-----------------|")
    for venue in target_venues:
        lines.append(
            f"| {venue} | {venue_counts_threshold.get(venue, 0)} | {venue_counts_top_k.get(venue, 0)} |"
        )
    lines.append("")

    # ------------------------------------------------------------------
    # 6.6. Papers excluidos del ranking por paper_blacklist
    # ------------------------------------------------------------------
    paper_blacklist = paper_blacklist or {}

    # Mapeo de motivos a indicadores numéricos
    REASON_TO_INDICATOR = {
        "low_text": "1",
        "operator_rejected": "2",
    }

    def _get_indicator(reason: str) -> str:
        return REASON_TO_INDICATOR.get(reason, "?")

    def _indicator_legend() -> str:
        return (
            "1: Bajo contenido de texto extraído (menos de 500 palabras tras "
            "limpieza en corpusCleaning)\n"
            "2: Excluido de selección manual (operador indicó 'N' durante la "
            "revisión de papers)"
        )

    # Construir lookup de títulos desde canonical.json si es posible
    title_lookup: dict[str, str] = {}
    canonical_file = output_dir / "canonical.json"
    if canonical_file.exists():
        try:
            with open(canonical_file, "r", encoding="utf-8") as f:
                canonical = json.load(f)
            if isinstance(canonical, list):
                for p in canonical:
                    pid = p.get("paper_id")
                    title = p.get("title")
                    if pid and title:
                        title_lookup[pid] = title
        except Exception:
            pass

    lines.append("## 6.6. Papers excluidos del ranking por paper_blacklist")
    lines.append("")
    lines.append(
        "Estos papers se excluyen automáticamente antes del ranking para no ocupar "
        "cupo de `top_k`. Cada uno tiene un indicador numérico que corresponde a "
        "el motivo de exclusión (ver leyenda al final de esta sección)."
    )
    lines.append("")
    lines.append(f"- Total de papers en blacklist: {len(paper_blacklist)}")
    lines.append("")

    if paper_blacklist:
        # Resumen por indicador
        indicator_counts: dict[str, int] = {}
        for pid, info in paper_blacklist.items():
            ind = _get_indicator(info.get("reason", "unknown"))
            indicator_counts[ind] = indicator_counts.get(ind, 0) + 1

        lines.append("### 6.6.1. Resumen por indicador")
        lines.append("")
        lines.append("| Indicador | Motivo | Nº papers |")
        lines.append("|-----------|--------|-----------|")
        for ind in sorted(indicator_counts.keys()):
            motivo = (
                "Bajo contenido de texto" if ind == "1"
                else "Excluido de selección manual" if ind == "2"
                else "Otro"
            )
            lines.append(f"| {ind} | {motivo} | {indicator_counts[ind]} |")
        lines.append("")

        lines.append("### 6.6.2. Lista detallada de papers blacklisteados")
        lines.append("")
        lines.append("| Paper ID | Título | Indicador |")
        lines.append("|----------|--------|-----------|")
        for pid in sorted(paper_blacklist.keys()):
            info = paper_blacklist[pid]
            title = title_lookup.get(pid) or info.get("detail", "")[:60] or "SIN TÍTULO"
            ind = _get_indicator(info.get("reason", "unknown"))
            lines.append(f"| {pid} | {title} | {ind} |")
        lines.append("")
        lines.append("### Leyenda de indicadores")
        lines.append("")
        for line in _indicator_legend().split("\n"):
            lines.append(f"- {line}")
        lines.append("")
        lines.append(
            "Nota: estos papers se excluyen automáticamente antes del ranking en "
            "ejecuciones futuras para no ocupar cupo de `top_k`."
        )
    lines.append("")

    # ------------------------------------------------------------------
    # 7. Flujo PRISMA resumido (formato clásico)
    # ------------------------------------------------------------------
    lines.append("## 7. Flujo PRISMA resumido")
    lines.append("")
    lines.append("```")
    lines.append("Registros identificados (todas las estrategias):  "
                 f"{stats['total_raw']}")
    lines.append("Registros tras deduplicación (canonical):        "
                 f"{canonical_count}")
    lines.append(f"Papers que superan el umbral de similitud:        {passed_threshold}")
    lines.append(f"Papers dentro del top_k (ranking):                {top_k_final}")
    lines.append(f"Papers fuera del top_k (superan umbral pero no entran): {outside_top_k_count}")
    lines.append("Papers seleccionados manualmente (review):         "
                 f"{selected_count}")
    lines.append("Papers con PDF adquirido:                          "
                 f"{len(acquired_list)}")
    lines.append("Papers incluidos en el corpus final:               "
                 f"{copied_count}")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def processCompleteSelection() -> Optional[Path]:
    """
    Selecciona los papers finales y genera el informe PRISMA (prisma.md).

    Este módulo reemplaza a `copySelectedPapers.py`. Además de copiar los PDFs
    de los papers seleccionados a la carpeta dedicada, escribe `prisma.md`
    con estadísticas exhaustivas del proceso de selección:

    - Registros por estrategia de búsqueda y fuente
    - Deduplicación (canonical.json)
    - Ranking y umbral de similitud (ranked_papers.json)
    - Los 30 primeros papers que NO pasan el umbral, con su similitud
    - Selección manual (selected_papers.json)
    - Adquisición de PDFs (selected_acquired.json, missing_pdfs.json)
    - Resumen final tipo PRISMA

    Retorna:
        Path a la carpeta results/output/<subject>/selected_papers/
    """
    input_dir, output_dir = inicioModulo("processCompleteSelection")
    subject = processControl.args.subject

    selected_file = output_dir / "selected_papers.json"
    pdfs_dir = output_dir / "pdfs"
    target_dir = output_dir / "selected_papers"

    if not selected_file.exists():
        writeLog("error", logger,
                 f"[CompleteSelection] No se encontró {selected_file}")
        raise FileNotFoundError(f"{selected_file} no existe")

    with open(selected_file, "r", encoding="utf-8") as f:
        selected = json.load(f)

    # Extraer paper_ids
    paper_ids = []
    if isinstance(selected, list):
        for p in selected:
            pid = str(p.get("paper_id", "")).strip()
            if pid:
                paper_ids.append(pid)
    else:
        writeLog("warning", logger,
                 f"[CompleteSelection] Formato inesperado en {selected_file}")
        return target_dir

    # --- Leer configuración para el PRISMA ---
    search_config = getattr(processControl, "defaults", {}) or {}
    search_cfg = search_config.get("search", {}) if isinstance(search_config, dict) else {}
    availables = search_cfg.get("availables", [])

    # --- LEER PAPER_BLACKLIST CONSOLIDADA ---
    # Combina: config.json entries + rechazos del operador + bajo contenido de texto.
    paper_blacklist = _load_paper_blacklist(output_dir)
    if paper_blacklist:
        writeLog("info", logger,
                 f"[CompleteSelection] {len(paper_blacklist)} papers en paper_blacklist "
                 f"(config + rechazos operador + bajo contenido de texto)")
    else:
        writeLog("info", logger,
                 f"[CompleteSelection] Ningún paper en paper_blacklist")

    # ------------------------------------------------------------------
    # 1. Copiar PDFs (funcionalidad original de copySelectedPapers)
    # ------------------------------------------------------------------
    if target_dir.exists():
        for item in target_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        writeLog("info", logger,
                 f"[CompleteSelection] Carpeta vaciada: {target_dir}")
    else:
        target_dir.mkdir(parents=True, exist_ok=True)
        writeLog("info", logger,
                 f"[CompleteSelection] Carpeta creada: {target_dir}")

    copied = []
    not_found = []
    for pid in paper_ids:
        src = pdfs_dir / f"{pid}.pdf"
        if src.exists():
            dst = target_dir / f"{pid}.pdf"
            shutil.copy2(src, dst)
            copied.append({"paper_id": pid, "pdf": f"{pid}.pdf"})
            writeLog("debug", logger,
                     f"[CompleteSelection] Copiado: {pid}.pdf")
        else:
            not_found.append(pid)
            writeLog("warning", logger,
                     f"[CompleteSelection] PDF no encontrado: {src}")

    # Guardar listado de papers copiados
    list_file = target_dir / "selected_papers_list.json"
    with open(list_file, "w", encoding="utf-8") as f:
        json.dump({
            "subject": subject,
            "total_seleccionados": len(paper_ids),
            "total_copiados": len(copied),
            "no_encontrados": not_found,
            "papers": copied,
        }, f, ensure_ascii=False, indent=2)

    writeLog("info", logger,
             f"[CompleteSelection] {len(copied)} PDFs copiados a "
             f"{target_dir} (solicitados: {len(paper_ids)}, no encontrados: "
             f"{len(not_found)})")

    # ------------------------------------------------------------------
    # 2. Generar prisma.md
    # ------------------------------------------------------------------
    # Cargar papers que superan el umbral (dentro de top_k)
    ranked_papers_file = output_dir / "ranked_papers.json"
    ranked_papers = _load_json(ranked_papers_file)
    if not isinstance(ranked_papers, list):
        ranked_papers = []

    # Cargar papers que superan el umbral pero se quedan fuera por top_k
    outside_top_k_file = output_dir / "ranking_outside_top_k.json"
    ranking_outside_top_k = _load_json(outside_top_k_file)
    if not isinstance(ranking_outside_top_k, list):
        ranking_outside_top_k = []

    # Cargar venues objetivo desde conceptsQuery.json del subject
    target_venues, venue_aliases = _load_target_venues(input_dir)

    prisma_md = _build_prisma_md(
        output_dir=output_dir,
        input_dir=input_dir,
        search_config=search_config,
        availables=availables,
        selected_count=len(paper_ids),
        copied_count=len(copied),
        not_found_count=len(not_found),
        ranked_papers=ranked_papers,
        ranking_outside_top_k=ranking_outside_top_k,
        paper_blacklist=paper_blacklist,
        target_venues=target_venues,
        venue_aliases=venue_aliases,
    )

    prisma_path = output_dir / "prisma.md"
    with open(prisma_path, "w", encoding="utf-8") as f:
        f.write(prisma_md)

    writeLog("info", logger,
             f"[CompleteSelection] Informe PRISMA escrito en {prisma_path}")

    return target_dir


if __name__ == "__main__":
    processCompleteSelection()
