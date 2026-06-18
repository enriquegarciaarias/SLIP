from sources.common.common import logger, processControl, writeLog

import json
import re
from pathlib import Path

import fitz


def clean_text(text):
    if not text:
        return ""
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_pdf_text(pdf_file):
    try:
        doc = fitz.open(pdf_file)
        pages = []
        for page in doc:
            txt = page.get_text()
            if txt:
                pages.append(txt)
        doc.close()
        return clean_text("\n".join(pages))
    except Exception as e:
        writeLog("error", logger, f"Error PDF {pdf_file}: {e}")
        return ""


SECTION_PATTERNS = {
    "abstract": r"\babstract\b",
    "introduction": r"\bintroduction\b",
    "methodology": r"\b(methodology|methods|materials and methods|approach)\b",
    "results": r"\b(results|experiments|evaluation)\b",
    "conclusion": r"\b(conclusion|conclusions|discussion and conclusion)\b"
}


def find_section_positions(text):
    positions = {}
    lower = text.lower()
    for section, pattern in SECTION_PATTERNS.items():
        m = re.search(pattern, lower)
        if m:
            positions[section] = m.start()
    return positions


def extract_sections(text):
    positions = find_section_positions(text)
    if not positions:
        return {}
    ordered = sorted(positions.items(), key=lambda x: x[1])
    sections = {}
    for i, (name, start) in enumerate(ordered):
        if i < len(ordered) - 1:
            end = ordered[i + 1][1]
        else:
            end = len(text)
        sections[name] = clean_text(text[start:end])
    return sections


def build_text_record(paper, pdf_file):
    """
    Construye SOLO el registro de texto (sin metadata redundante).
    """
    full_text = extract_pdf_text(pdf_file)
    sections = extract_sections(full_text)

    return {
        "paper_id": paper.get("paper_id"),  # ← Usar el paper_id original
        "full_text": full_text,
        "sections": sections,
        "pdf_file": str(pdf_file),
        "word_count": len(full_text.split()),
        "char_count": len(full_text)
    }


def build_metadata_record(paper):
    """
    Construye el registro de metadatos (sin texto).
    Solo campos útiles y no redundantes.
    """
    return {
        "paper_id": paper.get("paper_id"),
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
        "selected": paper.get("selected", True),
        # Opcional: añadir word_count si quieres tenerlo aquí también
        "word_count": paper.get("word_count"),
        "char_count": paper.get("char_count"),
        "has_quality_issues": paper.get("has_quality_issues", False)
    }


# Configuración de calidad mínima del texto
MIN_WORD_COUNT = 300   # mínimo de palabras para considerar el paper útil
MIN_CHAR_COUNT = 1500  # mínimo de caracteres (opcional)

def full_text_engine(selected_papers, pdf_dir):
    """
    Procesa los PDFs y genera dos estructuras separadas:
    - metadata_records: información bibliográfica
    - text_records: texto completo y secciones

    AHORA filtra papers con texto insuficiente.
    """
    metadata_records = []
    text_records = []
    pdf_dir = Path(pdf_dir)

    paper_index = {paper.get("paper_id"): paper for paper in selected_papers}

    for paper_id, paper in paper_index.items():
        pdf_file = pdf_dir / f"{paper_id}.pdf"
        if not pdf_file.exists():
            writeLog("warning", logger, f"[Extraction] No encontrado PDF para {paper_id}")
            continue

        writeLog("info", logger, f"[{paper_id}] Extrayendo {pdf_file.name}")

        # Extraer texto completo
        full_text = extract_pdf_text(pdf_file)
        word_count = len(full_text.split())
        char_count = len(full_text)

        # 🔥 FILTRO DE CALIDAD: si el texto es insuficiente, se omite el paper
        if word_count < MIN_WORD_COUNT or char_count < MIN_CHAR_COUNT:
            writeLog("warning", logger,
                     f"[Extraction] Paper {paper_id} descartado: texto muy corto "
                     f"(palabras={word_count}, caracteres={char_count})")
            continue

        # Extraer secciones
        sections = extract_sections(full_text)

        # Construir registros
        metadata_records.append(build_metadata_record(paper))
        text_records.append({
            "paper_id": paper_id,
            "full_text": full_text,
            "sections": sections,
            "pdf_file": str(pdf_file),
            "word_count": word_count,
            "char_count": char_count
        })

    return metadata_records, text_records


def processFullTextExtraction():
    writeLog("info", logger, "🚀 [START] Processing processFullTextExtraction")

    base_input_dir = Path(processControl.env.get("input", ""))
    base_output_dir = Path(processControl.env.get("output", ""))
    subject = processControl.args.subject

    output_dir = base_output_dir / subject

    selected_file = output_dir / "selected_papers.json"
    pdf_dir = output_dir / "pdfs"

    if not selected_file.exists():
        writeLog("error", logger, f"Not found: {selected_file}")
        return None

    with open(selected_file, "r", encoding="utf-8") as f:
        selected_papers = json.load(f)

    writeLog("info", logger, f"[Extraction] Loaded {len(selected_papers)} selected papers")

    # Procesar y separar
    metadata_records, text_records = full_text_engine(selected_papers, pdf_dir)

    # Guardar metadatos
    metadata_file = output_dir / "papers_metadata.json"
    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata_records, f, indent=2, ensure_ascii=False)

    # Guardar texto
    text_file = output_dir / "papers_text.json"
    with open(text_file, "w", encoding="utf-8") as f:
        json.dump(text_records, f, indent=2, ensure_ascii=False)

    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, "EXTRACCIÓN COMPLETADA")
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, f"  Metadatos: {len(metadata_records)} registros → {metadata_file}")
    writeLog("info", logger, f"  Texto:     {len(text_records)} registros → {text_file}")
    writeLog("info", logger, "✅ [END] processFullTextExtraction")

    return metadata_records, text_records


if __name__ == "__main__":
    processFullTextExtraction()