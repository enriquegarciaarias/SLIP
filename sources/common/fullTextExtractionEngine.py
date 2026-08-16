# fullTextExtractionEngine.py
"""
Servicio Federado de Extracción de Texto.
Responsabilidad Única: Recibir una ruta a un PDF y devolver su texto y secciones.
No conoce nada sobre el pipeline, metadatos ni JSONs de entrada/salida.
"""

import re
import fitz
from sources.common.common import logger, writeLog


def _clean_pdf_text(text):
    """Limpieza básica a nivel de extractor (null bytes, espacios múltiples)."""
    if not text:
        return ""
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


SECTION_PATTERNS = {
    "abstract": r"\babstract\b",
    "introduction": r"\bintroduction\b",
    "methodology": r"\b(methodology|methods|materials and methods|approach)\b",
    "results": r"\b(results|experiments|evaluation)\b",
    "conclusion": r"\b(conclusion|conclusions|discussion and conclusion)\b"
}


def _find_section_positions(text):
    positions = {}
    lower = text.lower()
    for section, pattern in SECTION_PATTERNS.items():
        m = re.search(pattern, lower)
        if m:
            positions[section] = m.start()
    return positions


def _extract_sections(text):
    positions = _find_section_positions(text)
    if not positions:
        return {}
    ordered = sorted(positions.items(), key=lambda x: x[1])
    sections = {}
    for i, (name, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i < len(ordered) - 1 else len(text)
        sections[name] = _clean_pdf_text(text[start:end])
    return sections


def _extract_page_text(page) -> str:
    """Extrae el texto de una página excluyendo el contenido de las tablas detectadas."""
    tables = page.find_tables()
    if not tables.tables:
        return page.get_text()

    table_bboxes = [fitz.Rect(t.bbox) for t in tables.tables]
    blocks = page.get_text("blocks")
    kept = []
    for block in blocks:
        x0, y0, x1, y1, text, _block_no, block_type = block
        if block_type != 0:
            continue
        bbox = fitz.Rect(x0, y0, x1, y1)
        if any(bbox.intersects(tb) for tb in table_bboxes):
            continue
        if text.strip():
            kept.append(text.strip())
    return "\n".join(kept)


def extract_full_text_service(pdf_path: str) -> dict:
    """
    INTERFAZ PÚBLICA DEL SERVICIO.

    Recibe: Ruta al archivo PDF.
    Devuelve: Diccionario con texto crudo y secciones, o vacío si falla.

    Ejemplo de uso desde otro módulo:
        resultado = extract_full_text_service("/ruta/al/paper.pdf")
        if resultado:
            print(resultado["word_count"])
    """
    try:
        doc = fitz.open(pdf_path)
        pages = []
        for page in doc:
            txt = _extract_page_text(page)
            if txt:
                pages.append(txt)
        doc.close()

        full_text = _clean_pdf_text("\n".join(pages))

        if not full_text:
            writeLog("warning", logger, f"PDF vacío o sin texto extraíble: {pdf_path}")
            return {}

        return {
            "full_text": full_text,
            "sections": _extract_sections(full_text),
            "word_count": len(full_text.split()),
            "char_count": len(full_text)
        }
    except Exception as e:
        writeLog("error", logger, f"Error extrayendo PDF {pdf_path}: {e}")
        return {}