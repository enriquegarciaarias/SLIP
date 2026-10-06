# corpusCleaning.py
"""
Orquestador de Limpieza de Corpus.
Responsabilidades:
  1. Leer selected_acquired.json (Estado del módulo anterior)
  2. Orquestar la extracción de texto usando el servicio federado
  3. Aplicar filtros de calidad
  4. Aplicar limpieza regex al texto
  5. Emitir papers_metadata.json y papers_text.json
"""

from sources.common.common import logger, processControl, writeLog
from sources.common.fullTextExtractionEngine import extract_full_text_service  # <-- Importamos el servicio
from sources.common.referenceFilter import strip_reference_tail
from sources.common.pdfIndex import load_pdf_index, resolve_paper_pdf
from sources.common.utils import inicioModulo

import re
import json
from pathlib import Path

# ==================================================
# CONFIGURACIÓN
# ==================================================
MIN_WORD_COUNT = 300  # Mínimo de palabras para considerar el paper útil
MIN_CHAR_COUNT = 1500  # Mínimo de caracteres


def load_corpus_cleaning_config() -> None:
    """
    Sobrescribe las constantes del módulo con la sección "corpusCleaning"
    de config.json (processControl.defaults). Los valores del fichero tienen
    prioridad; si faltan, se conservan los valores por defecto.
    """
    defaults = getattr(processControl, "defaults", None) or {}
    cfg = defaults.get("corpusCleaning", {}) if isinstance(defaults, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}

    global MIN_WORD_COUNT, MIN_CHAR_COUNT
    MIN_WORD_COUNT = int(cfg.get("min_word_count", MIN_WORD_COUNT))
    MIN_CHAR_COUNT = int(cfg.get("min_char_count", MIN_CHAR_COUNT))


# ==================================================
# REGEX COMPILADAS (Limpieza de Corpus)
# ==================================================
RE_URL = re.compile(r"http\S+|www\.\S+", re.IGNORECASE)
RE_EMAIL = re.compile(r"\S+@\S+")
RE_HYPHEN_NEWLINE = re.compile(r"(\w+)-\s*\n\s*(\w+)")
RE_HYPHEN_SPACE = re.compile(r"(\w+)-\s+(\w+)")
RE_COPYRIGHT = re.compile(r"©.*?(?:\n|$)", re.IGNORECASE | re.DOTALL)
RE_ALL_RIGHTS = re.compile(r"all rights reserved.*?(?:\n|$)", re.IGNORECASE | re.DOTALL)
RE_SECTION_HEADER = re.compile(
    r"^\s*(?:references|bibliography|acknowledgments?|funding|acknowledgements?)\s*$",
    re.IGNORECASE | re.MULTILINE
)
RE_MULTISPACE = re.compile(r"\s+")
RE_PUNCTUATION_SPACE = re.compile(r"\s+([.,;:!?])")
RE_PAGE_NUMBER = re.compile(r"^\d+$")


def clean_text_corpus(text: str) -> str:
    """Limpieza profunda a nivel de corpus (URLs, emails, referencias finales...)."""
    if not text:
        return ""

    text = RE_URL.sub(" ", text)
    text = RE_EMAIL.sub(" ", text)
    text = RE_HYPHEN_NEWLINE.sub(r"\1\2", text)
    text = RE_HYPHEN_SPACE.sub(r"\1\2", text)
    text = text.replace("\r", "\n")

    # Cortar a partir de referencias/bibliografía para no contaminar el corpus
    cut_pos = len(text)
    for m in RE_SECTION_HEADER.finditer(text):
        cut_pos = min(cut_pos, m.start())
    text = text[:cut_pos]

    # Segundo corte robusto: encabezados de bibliografía inline (el texto
    # puede venir sin saltos de línea tras la extracción del PDF).
    text = strip_reference_tail(text)

    text = RE_COPYRIGHT.sub(" ", text)
    text = RE_ALL_RIGHTS.sub(" ", text)
    text = RE_PUNCTUATION_SPACE.sub(r"\1", text)

    # Eliminar líneas que son solo números de página
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if RE_PAGE_NUMBER.match(line):
            continue
        if len(line) < 2:
            continue
        lines.append(line)

    text = "\n".join(lines)
    text = RE_MULTISPACE.sub(" ", text)

    return text.strip()


def processCorpusCleaning():
    load_corpus_cleaning_config()
    input_dir, output_dir = inicioModulo("processCorpusCleaning")

    # -------------------------------------------------------------
    # 1. ENTRADA: Leer el estado de adquisición (nuevo estándar)
    # -------------------------------------------------------------
    acquired_file = output_dir / "selected_acquired.json"
    if not acquired_file.exists():
        writeLog("error", logger, f"No encontrado: {acquired_file}. Ejecuta primero processAcquisitionEngine.")
        return None

    with open(acquired_file, "r", encoding="utf-8") as f:
        acquired_papers = json.load(f)

    writeLog("info", logger, f"[CLEAN] Leídos {len(acquired_papers)} papers de selected_acquired.json")

    # Índice determinista paper_id -> PDF construido en la adquisición
    pdf_index = load_pdf_index(output_dir)
    if pdf_index:
        writeLog("info", logger, f"[CLEAN] Índice de PDFs cargado: {len(pdf_index)} entradas")

    # -------------------------------------------------------------
    # 2. PROCESAMIENTO: Extracción + Limpieza + Filtrado
    # -------------------------------------------------------------
    cleaned_metadata = []
    cleaned_texts = []
    discarded_count = 0

    for paper in acquired_papers:
        paper_id = paper.get("paper_id", "unknown")
        status = paper.get("acquisition_status")

        # Asociación determinista vía índice (pdf_download_log.json);
        # `local_pdf_path` queda como respaldo para ejecuciones antiguas.
        pdf_file = resolve_paper_pdf(paper, output_dir, pdf_index)

        # Filtro de estado: Solo procesamos los que tienen PDF real
        if not pdf_file or not pdf_file.exists():
            if status in [None, "missing", "pending"]:
                continue
            writeLog("warning", logger, f"[{paper_id}] PDF no encontrado en disco: {pdf_file}")
            continue

        writeLog("info", logger, f"[{paper_id}] Extrayendo y limpiando...")

        # --- PASO A: Extracción (Servicio Federado) ---
        extraction_result = extract_full_text_service(str(pdf_file))

        if not extraction_result:
            discarded_count += 1
            continue

        raw_word_count = extraction_result.get("word_count", 0)
        raw_char_count = extraction_result.get("char_count", 0)

        # --- PASO B: Filtro de Calidad ---
        if raw_word_count < MIN_WORD_COUNT or raw_char_count < MIN_CHAR_COUNT:
            writeLog("warning", logger, f"[{paper_id}] Descartado por texto insuficiente ({raw_word_count} palabras)")
            discarded_count += 1
            continue

        # --- PASO C: Limpieza de Corpus (Regex) ---
        clean_full = clean_text_corpus(extraction_result.get("full_text", ""))
        clean_sections = {
            sec_name: clean_text_corpus(sec_text)
            for sec_name, sec_text in extraction_result.get("sections", {}).items()
        }

        final_word_count = len(clean_full.split())
        final_char_count = len(clean_full)

        # --- PASO D: Construcción de Registros de Salida ---

        # Registro de Metadatos
        metadata_record = {
            "paper_id": paper_id,
            "title": paper.get("title"),
            "doi": paper.get("doi"),
            "abstract": paper.get("abstract"),
            "authors": paper.get("authors", []),
            "keywords": paper.get("keywords", []),
            "year": paper.get("year"),
            "venue": paper.get("venue"),
            "citation_count": paper.get("citation_count"),
            "relevance_score": paper.get("relevance_score"),
            "is_oa": paper.get("is_oa"),
            "pdf_url": paper.get("pdf_url"),
            "pdf_source": paper.get("pdf_source"),
            # Estadísticas finales post-limpieza
            "word_count": final_word_count,
            "char_count": final_char_count,
            "has_quality_issues": final_word_count < 500  # Umbral de calidad bajo
        }
        cleaned_metadata.append(metadata_record)

        # Registro de Texto
        text_record = {
            "paper_id": paper_id,
            "clean_text": clean_full,
            "clean_sections": clean_sections,
            "pdf_file": str(pdf_file),
            "word_count": final_word_count,
            "char_count": final_char_count
            # Nota: No guardamos "full_text" para ahorrar espacio en disco
        }
        cleaned_texts.append(text_record)

    # -------------------------------------------------------------
    # 3. SALIDA: Guardar los nuevos contratos de datos
    # -------------------------------------------------------------
    metadata_file = output_dir / "papers_metadata.json"
    text_file = output_dir / "papers_text.json"

    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(cleaned_metadata, f, indent=2, ensure_ascii=False)

    with open(text_file, "w", encoding="utf-8") as f:
        json.dump(cleaned_texts, f, indent=2, ensure_ascii=False)

    # -------------------------------------------------------------
    # 4. RESUMEN
    # -------------------------------------------------------------
    total_papers = len(cleaned_metadata)
    total_words = sum(p.get("word_count", 0) for p in cleaned_metadata)
    low_quality = sum(1 for p in cleaned_metadata if p.get("has_quality_issues", False))

    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, "CORPUS CLEANING COMPLETED")
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, f"  Procesados desde selected_acquired: {len(acquired_papers)}")
    writeLog("info", logger, f"  Descartados (sin PDF o texto corto): {discarded_count}")
    writeLog("info", logger, f"  papers_metadata.json: {total_papers} registros válidos")
    writeLog("info", logger, f"  papers_text.json: {total_papers} registros válidos")
    writeLog("info", logger, f"  Total palabras limpias: {total_words:,}")
    writeLog("info", logger, f"  Low quality (<500 palabras): {low_quality}")
    writeLog("info", logger, "✅ [END] processCorpusCleaning")

    return cleaned_metadata, cleaned_texts


if __name__ == "__main__":
    processCorpusCleaning()