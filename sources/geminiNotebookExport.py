#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
geminiNotebookExport.py
=======================

Exporta los papers seleccionados del pipeline SLIP a archivos Markdown
optimizados para subir como fuentes en Google Gemini Notebook.

Límites por cuaderno (archivo Markdown):
  - Máximo 50 papers (fuentes) por archivo.
  - Máximo 200 MB por archivo.
  - Cada paper se trunca a 500 000 palabras como máximo.

Los papers se ordenan por relevance_score (ranked_papers.json) de modo que
los más relevantes queden en los primeros archivos.

Se genera hasta 50 archivos (cuadernos), recorriendo todos los papers
seleccionados por orden de importancia.

Salida: results/output/<subject>/notebook/gemini_notebook_01.md, ..._02.md, ...
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo, read_json


# ---------------------------------------------------------------------------
# Límites configurables
# ---------------------------------------------------------------------------
MAX_PAPERS_PER_NOTEBOOK = 50      # fuentes por cuaderno
MAX_NOTEBOOKS = 50                # número máximo de archivos (cuadernos)
MAX_FILE_BYTES = 200 * 1024 * 1024  # 200 MB por archivo
MAX_WORDS_PER_PAPER = 500_000     # palabras por fuente (paper)


def _slugify_title(title: str, paper_id: str) -> str:
    """Crea una etiqueta segura para el encabezado del paper."""
    clean = re.sub(r"[^\w\s-]", "", title or "").strip()
    clean = re.sub(r"\s+", " ", clean)
    return f"{paper_id}-{clean[:80]}"


def _count_words(text: str) -> int:
    return len(text.split())


def _truncate_to_words(text: str, max_words: int) -> str:
    """Trunca el texto a un máximo de palabras, preservando palabras completas."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def _load_selected_papers(output_dir: Path) -> list[dict]:
    """Carga los papers seleccionados (con PDF adquirido si existe)."""
    acquired = read_json(output_dir / "selected_acquired.json")
    if isinstance(acquired, list) and acquired:
        return acquired
    selected = read_json(output_dir / "selected_papers.json")
    if isinstance(selected, list) and selected:
        return selected
    return []


def _load_ranking(output_dir: Path) -> dict[str, float]:
    """Devuelve un diccionario paper_id -> relevance_score desde ranked_papers.json."""
    ranked = read_json(output_dir / "ranked_papers.json")
    scores: dict[str, float] = {}
    if isinstance(ranked, list):
        for p in ranked:
            pid = p.get("paper_id")
            score = p.get("relevance_score", 0.0)
            if pid is not None:
                scores[pid] = float(score) if isinstance(score, (int, float)) else 0.0
    return scores


def _load_papers_text(output_dir: Path) -> dict[str, dict]:
    """Carga papers_text.json indexado por paper_id."""
    raw = read_json(output_dir / "papers_text.json")
    lookup: dict[str, dict] = {}
    if isinstance(raw, list):
        for p in raw:
            pid = p.get("paper_id")
            if pid is not None:
                lookup[pid] = p
    return lookup


def _build_paper_block(paper: dict, text_lookup: dict[str, dict],
                       score: float) -> str:
    """Construye el bloque Markdown de un solo paper."""
    paper_id = paper.get("paper_id", "desconocido")
    title = paper.get("title") or ""

    # Obtener texto completo: priorizar clean_text, luego unir secciones
    pt = text_lookup.get(paper_id, {})
    clean_text = pt.get("clean_text") or ""

    if not clean_text.strip():
        sections = pt.get("clean_sections", {}) or {}
        parts = []
        for section_name in ("abstract", "introduction", "methodology",
                             "results", "conclusion", "figures_tables_captions"):
            val = sections.get(section_name)
            if val:
                parts.append(f"## {section_name.capitalize()}\n{val}")
        if parts:
            clean_text = "\n\n".join(parts)

    if not clean_text.strip():
        # Último recurso: abstract del paper
        clean_text = paper.get("abstract") or ""

    # Truncar a 500 000 palabras
    clean_text = _truncate_to_words(clean_text, MAX_WORDS_PER_PAPER)

    n_words = _count_words(clean_text)
    label = _slugify_title(title, paper_id)

    block = [
        f"TITULO DEL PAPER: {label}\n",
        f"Paper ID: {paper_id}\n",
        f"Relevance Score: {score:.4f}\n",
        f"Fuente: {paper.get('source', '')}\n",
        f"Palabras: {n_words}\n",
        "\n---\n\n",
        clean_text,
        "\n",
    ]
    return "\n".join(block)


def processGeminiNotebookExport() -> dict[str, Any]:
    """
    Genera los archivos Markdown para Gemini Notebook.

    Retorna un diccionario con estadísticas de la exportación.
    """
    input_dir, output_dir = inicioModulo("processGeminiNotebookExport")
    subject = processControl.args.subject

    notebook_dir = output_dir / "notebook"
    notebook_dir.mkdir(parents=True, exist_ok=True)

    # --- Cargar datos ---
    selected_papers = _load_selected_papers(output_dir)
    if not selected_papers:
        writeLog("warning", logger,
                 "[GeminiNotebookExport] No hay papers seleccionados para exportar.")
        return {"notebooks_generated": 0, "papers_exported": 0}

    score_lookup = _load_ranking(output_dir)
    text_lookup = _load_papers_text(output_dir)

    writeLog("info", logger,
             f"[GeminiNotebookExport] {len(selected_papers)} papers seleccionados, "
             f"{len(score_lookup)} con score de ranking, "
             f"{len(text_lookup)} con texto en papers_text.json")

    # --- Ordenar por relevance_score descendente ---
    def _sort_key(p: dict) -> float:
        pid = p.get("paper_id", "")
        return score_lookup.get(pid, 0.0)

    selected_papers.sort(key=_sort_key, reverse=True)

    # --- Construir bloques y agrupar en archivos ---
    paper_blocks: list[str] = []
    paper_meta: list[dict] = []

    for paper in selected_papers:
        pid = paper.get("paper_id", "desconocido")
        score = score_lookup.get(pid, 0.0)
        block = _build_paper_block(paper, text_lookup, score)
        paper_blocks.append(block)
        paper_meta.append({
            "paper_id": pid,
            "title": paper.get("title", ""),
            "source": paper.get("source", ""),
            "relevance_score": score,
            "words": _count_words(block),
        })

    total_papers = len(paper_blocks)
    writeLog("info", logger,
             f"[GeminiNotebookExport] {total_papers} bloques de paper construidos")

    # --- Particionar en archivos respetando límites ---
    notebooks: list[list[str]] = []
    current: list[str] = []
    current_words = 0
    current_bytes = 0

    for i, block in enumerate(paper_blocks):
        block_words = _count_words(block)
        block_bytes = len(block.encode("utf-8"))

        # ¿Excedería este paper los límites del archivo actual?
        would_exceed_words = (current_words + block_words) > MAX_WORDS_PER_PAPER * MAX_PAPERS_PER_NOTEBOOK
        would_exceed_bytes = (current_bytes + block_bytes) > MAX_FILE_BYTES
        would_exceed_count = len(current) >= MAX_PAPERS_PER_NOTEBOOK

        if current and (would_exceed_count or would_exceed_bytes or would_exceed_words):
            notebooks.append(current)
            current = [block]
            current_words = block_words
            current_bytes = block_bytes
        else:
            current.append(block)
            current_words += block_words
            current_bytes += block_bytes

        # Límite máximo de archivos (cuadernos)
        if len(notebooks) >= MAX_NOTEBOOKS:
            # Si aún queda espacio en el archivo actual, seguir llenándolo
            # pero no crear más archivos nuevos después de este
            pass

    if current:
        notebooks.append(current)

    # Si se excedió el número máximo de cuadernos, truncar
    if len(notebooks) > MAX_NOTEBOOKS:
        writeLog("warning", logger,
                 f"[GeminiNotebookExport] Se truncan de {len(notebooks)} a "
                 f"{MAX_NOTEBOOKS} cuadernos (límite máximo)")
        notebooks = notebooks[:MAX_NOTEBOOKS]

    # --- Escribir archivos ---
    written_files: list[dict] = []
    total_exported = 0

    for idx, nb_blocks in enumerate(notebooks, start=1):
        filename = f"gemini_notebook_{idx:02d}.md"
        filepath = notebook_dir / filename
        content = "\n\n".join(nb_blocks) + "\n"

        file_size_mb = len(content.encode("utf-8")) / (1024 * 1024)
        total_words = sum(_count_words(b) for b in nb_blocks)

        filepath.write_text(content, encoding="utf-8")
        total_exported += len(nb_blocks)

        written_files.append({
            "file": filename,
            "path": str(filepath),
            "papers": len(nb_blocks),
            "size_mb": round(file_size_mb, 2),
            "words": total_words,
        })

        writeLog("info", logger,
                 f"[GeminiNotebookExport] {filename}: {len(nb_blocks)} papers, "
                 f"{file_size_mb:.2f} MB, {total_words} palabras")

    resumen = {
        "notebooks_generated": len(written_files),
        "papers_exported": total_exported,
        "total_selected": total_papers,
        "max_papers_per_notebook": MAX_PAPERS_PER_NOTEBOOK,
        "max_notebooks": MAX_NOTEBOOKS,
        "max_file_bytes": MAX_FILE_BYTES,
        "max_words_per_paper": MAX_WORDS_PER_PAPER,
        "files": written_files,
    }

    # Guardar resumen de exportación
    summary_file = notebook_dir / "gemini_notebook_export_summary.json"
    summary_file.write_text(json.dumps(resumen, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    writeLog("info", logger,
             f"[GeminiNotebookExport] {len(written_files)} cuadernos generados, "
             f"{total_exported} papers exportados a {notebook_dir}")
    writeLog("info", logger, "✅ [END] processGeminiNotebookExport")

    return resumen


if __name__ == "__main__":
    processGeminiNotebookExport()
