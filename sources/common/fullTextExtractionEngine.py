# fullTextExtractionEngine.py
"""
Servicio Federado de Extracción de Texto.
Responsabilidad Única: Recibir una ruta a un PDF y devolver su texto y secciones.
No conoce nada sobre el pipeline, metadatos ni JSONs de entrada/salida.
"""

import re
import fitz
from typing import Dict, List
from sources.common.referenceFilter import strip_reference_tail
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
    "conclusion": r"\b(conclusion|conclusions|discussion and conclusion)\b",
    "references": r"\b(references|bibliography|works cited|literature cited|references and notes)\b"
}

# Regex para detectar captions de tabla y figura (ej: "Table 2: ...", "Fig. 1: ...")
# Diseñado para texto en línea continua (sin saltos de línea), como el producido por
# _clean_pdf_text. Matchea hasta el siguiente caption o fin de texto.
CAPTION_RE = re.compile(
    r'(Table\s+\d+[:\.]|TABLE\s+\d+[:\.]|Fig\.\s*\d+[:\.]|FIG\.\s*\d+[:\.]|Figure\s+\d+[:\.]|FIGURE\s+\d+[:\.])\s*(.+?)(?=\s+(?:Table\s+\d+[:\.]|TABLE\s+\d+[:\.]|Fig\.\s*\d+[:\.]|FIG\.\s*\d+[:\.]|Figure\s+\d+[:\.]|FIGURE\s+\d+[:\.])\s|$)',
    re.IGNORECASE
)


def _find_section_positions(text):
    positions = {}
    lower = text.lower()
    for section, pattern in SECTION_PATTERNS.items():
        m = re.search(pattern, lower)
        if m:
            positions[section] = m.start()
    return positions


def _extract_sections(text, positions: Dict[str, int] = None):
    if positions is None:
        positions = _find_section_positions(text)
    if not positions:
        return {}
    ordered = sorted(positions.items(), key=lambda x: x[1])
    sections = {}
    for i, (name, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i < len(ordered) - 1 else len(text)
        sections[name] = _clean_pdf_text(text[start:end])
    return sections


def _extract_captions_from_text(text: str, positions: Dict[str, int]) -> List[Dict]:
    """
    Extrae captions de tabla y figura del texto completo y asigna cada uno a su
    sección basándose en el offset del carácter dentro del texto.

    Devuelve lista de dicts con: tipo, label, texto, seccion, offset.
    """
    ordered_sections = sorted(positions.items(), key=lambda x: x[1])
    captions = []
    for m in CAPTION_RE.finditer(text):
        label = m.group(1).strip()
        caption_text = m.group(2).strip()
        if len(caption_text) <= 8:
            continue
        offset = m.start()
        tipo = 'table' if 'table' in label.lower() else 'figure'
        # Determinar sección por offset (la última sección cuyo inicio <= offset)
        section = "unknown"
        for name, start in ordered_sections:
            if start <= offset:
                section = name
            else:
                break
        captions.append({
            "tipo": tipo,
            "label": label,
            "texto": caption_text,
            "seccion": section,
            "offset": offset,
        })
    return captions


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

        # Remove bibliography/reference tail before sectioning
        clean_full = strip_reference_tail(full_text)

        # Posiciones de sección (calculadas una vez y reutilizadas para captions)
        section_positions = _find_section_positions(clean_full)
        sections = _extract_sections(clean_full, section_positions)

        # Extraer captions de tabla y figura con su sección asignada
        captions = _extract_captions_from_text(clean_full, section_positions)

        if captions:
            writeLog("info", logger,
                     f"[FullText] {len(captions)} captions extraídos de {pdf_path}")

        return {
            "full_text": clean_full,
            "sections": sections,
            "captions": captions,
            "word_count": len(clean_full.split()),
            "char_count": len(clean_full)
        }
    except Exception as e:
        writeLog("error", logger, f"Error extrayendo PDF {pdf_path}: {e}")
        return {}