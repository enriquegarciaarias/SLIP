from sources.common.common import logger, processControl, writeLog

import re
import json
from pathlib import Path

# ==================================================
# REGEX COMPILADAS
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


def clean_text(text: str) -> str:
    """Limpia el texto de un paper científico."""
    if not text:
        return ""

    text = RE_URL.sub(" ", text)
    text = RE_EMAIL.sub(" ", text)
    text = RE_HYPHEN_NEWLINE.sub(r"\1\2", text)
    text = RE_HYPHEN_SPACE.sub(r"\1\2", text)
    text = text.replace("\r", "\n")

    # Secciones finales
    cut_pos = len(text)
    for m in RE_SECTION_HEADER.finditer(text):
        cut_pos = min(cut_pos, m.start())
    text = text[:cut_pos]

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
    """Entry point principal."""
    writeLog("info", logger, "🚀 [START] Processing corpusCleaning")
    base_input_dir = Path(processControl.env.get("input", ""))
    base_output_dir = Path(processControl.env.get("output", ""))
    subject = processControl.args.subject
    input_dir = base_input_dir / subject
    output_dir = base_output_dir / subject

    # Archivos de entrada
    metadata_file = output_dir / "papers_metadata.json"
    text_file = output_dir / "papers_text.json"

    # Cargar datos
    with open(metadata_file, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    with open(text_file, "r", encoding="utf-8") as f:
        texts = json.load(f)

    writeLog("info", logger, f"[CLEAN] Procesando {len(texts)} papers")

    # Crear índice de metadatos por paper_id
    metadata_index = {p["paper_id"]: p for p in metadata}

    # Limpiar textos y actualizar metadata con estadísticas
    cleaned_metadata = []
    cleaned_texts = []

    for text_paper in texts:
        paper_id = text_paper["paper_id"]
        meta = metadata_index.get(paper_id, {})

        # Limpiar texto
        raw_text = text_paper.get("full_text", "")
        clean = clean_text(raw_text)
        word_count = len(clean.split())
        char_count = len(clean)

        # Limpiar secciones si existen
        cleaned_sections = {}
        for section_name, section_text in text_paper.get("sections", {}).items():
            cleaned_sections[section_name] = clean_text(section_text)

        # Actualizar metadata con estadísticas de texto
        meta["word_count"] = word_count
        meta["char_count"] = char_count
        meta["has_quality_issues"] = word_count < 500

        cleaned_metadata.append(meta)

        # Actualizar texto limpio
        text_paper["clean_text"] = clean
        text_paper["clean_sections"] = cleaned_sections
        text_paper["word_count"] = word_count
        text_paper["char_count"] = char_count
        # Eliminar full_text original para ahorrar espacio
        if "full_text" in text_paper:
            del text_paper["full_text"]

        cleaned_texts.append(text_paper)

        writeLog("info", logger, f"[CLEAN] {paper_id}: {word_count} palabras")

    # Guardar archivos actualizados (sobrescribir)
    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(cleaned_metadata, f, indent=2, ensure_ascii=False)

    with open(text_file, "w", encoding="utf-8") as f:
        json.dump(cleaned_texts, f, indent=2, ensure_ascii=False)

    # Estadísticas rápidas
    total_papers = len(cleaned_metadata)
    total_words = sum(p.get("word_count", 0) for p in cleaned_metadata)
    low_quality = sum(1 for p in cleaned_metadata if p.get("has_quality_issues", False))

    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, "CORPUS CLEANING COMPLETED")
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, f"  papers_metadata.json: {total_papers} papers (actualizado)")
    writeLog("info", logger, f"  papers_text.json: {total_papers} papers (texto limpio)")
    writeLog("info", logger, f"  Total palabras: {total_words:,}")
    writeLog("info", logger, f"  Low quality (<500 palabras): {low_quality}")

    return cleaned_metadata, cleaned_texts


if __name__ == "__main__":
    processCorpusCleaning()