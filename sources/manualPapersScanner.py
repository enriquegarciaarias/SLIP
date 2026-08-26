# manualPapersScanner.py
"""
Módulo de Detección Automática de PDFs Manuales.
Responsabilidad: Escanear input/{subject}/manual_papers/, extraer metadatos de cada
PDF (título, DOI, año, autores, abstract) y crear/actualizar de forma automática el
fichero manual_papers.json que consume processManualIngestion.

Flujo:
  1. Lista los *.pdf de input/{subject}/manual_papers/.
  2. Para cada PDF extrae metadatos mediante heurísticas (sin LLM).
  3. Fusiona con el manual_papers.json existente:
       - Conserva las entradas ya presentes (y sus ediciones manuales).
       - Rellena únicamente los campos vacíos con los metadatos extraídos.
       - Añade entradas para los PDFs nuevos.
  4. Escribe manual_papers.json.

Idempotente: ejecutarlo varias veces no duplica entradas ni pisa metadatos manuales.
"""

import json
import re
from pathlib import Path

import fitz

from sources.common.common import logger, writeLog
from sources.common.utils import inicioModulo, read_json, write_json


# ---------------------------------------------------------------------------
# Extracción heurística de metadatos desde el PDF
# ---------------------------------------------------------------------------

_DOI_PATTERN = re.compile(r"(?:https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/[^\s\"<>]+)")
_YEAR_PATTERN = re.compile(r"\b(20\d{2})\b")
_PDF_DATE_PATTERN = re.compile(r"D:(\d{4})")
_JOURNAL_HEADER = re.compile(r"\(20\d{2}\)\s*\d+\s*[:–-]")
_TITLE_NOISE = (
    "journal homepage",
    "contents lists available",
    "sciencedirect",
    "elsevier",
    "springer",
    "doi.org",
    "issn",
    "homepage:",
)
_ABSTRACT_PATTERN = re.compile(
    r"\babstract\b\s*[-:.]?\s*(.+?)(?=\n\s*(?:1\.?\s*(?:introduction|introducci|background)|keywords?\b|index\s+terms\b|\Z))",
    re.IGNORECASE | re.DOTALL,
)


def _clean(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _strip_doi(doi: str) -> str:
    doi = doi.strip().rstrip(".,;:()'\">")
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/)", "", doi, flags=re.IGNORECASE)
    return doi


def _extract_title(text: str, raw_meta: dict, filename: str) -> str:
    title = _clean(raw_meta.get("title") or "")
    if title:
        return title
    for line in text.splitlines()[:30]:
        line = line.strip()
        if len(line) <= 25 or re.fullmatch(r"[\d\s]+", line):
            continue
        low = line.lower()
        if _JOURNAL_HEADER.search(line):
            continue
        if any(tok in low for tok in _TITLE_NOISE):
            continue
        return line
    return filename


def _extract_abstract(text: str) -> str:
    match = _ABSTRACT_PATTERN.search(text)
    if not match:
        return ""
    abstract = _clean(match.group(1))
    if len(abstract) < 40:
        return ""
    return abstract[:2000]


def extract_pdf_metadata(pdf_path: Path) -> dict:
    """Extrae título, DOI, año, autores y abstract de un PDF mediante heurísticas."""
    meta = {"title": None, "doi": None, "year": None, "authors": [], "abstract": None}
    try:
        with fitz.open(pdf_path) as doc:
            raw_meta = doc.metadata or {}
            text = "".join(page.get_text() for page in doc)
    except Exception as exc:
        writeLog("error", logger, f"[ManualScanner] No se pudo leer {pdf_path.name}: {exc}")
        return meta

    meta["title"] = _extract_title(text, raw_meta, pdf_path.stem)

    match = _DOI_PATTERN.search(text)
    if match:
        meta["doi"] = _strip_doi(match.group(1))

    creation = raw_meta.get("creationDate") or ""
    match = _PDF_DATE_PATTERN.search(creation)
    if match:
        meta["year"] = int(match.group(1))
    else:
        match = _YEAR_PATTERN.search(text)
        if match:
            meta["year"] = int(match.group(1))

    authors_raw = _clean(raw_meta.get("author") or "")
    if authors_raw:
        meta["authors"] = [a.strip() for a in re.split(r"[;,]|\band\b", authors_raw) if a.strip()]

    abstract = _extract_abstract(text)
    if abstract:
        meta["abstract"] = abstract

    return meta


# ---------------------------------------------------------------------------
# Escaneo + fusión con manual_papers.json
# ---------------------------------------------------------------------------

def _merge_entry(existing: dict, detected: dict) -> bool:
    """Rellena campos vacíos con los detectados. Devuelve True si hubo cambios."""
    changed = False
    for field, value in detected.items():
        if value is None or value == []:
            continue
        current = existing.get(field)
        if field == "authors":
            if not current:
                existing[field] = value
                changed = True
        elif field == "abstract":
            if not current:
                existing[field] = value
                changed = True
        elif not current:
            existing[field] = value
            changed = True
    return changed


def _load_existing(manual_json: Path) -> dict:
    if not manual_json.exists():
        return {}
    try:
        data = read_json(manual_json)
    except Exception as exc:
        writeLog("warning", logger, f"[ManualScanner] No se pudo leer {manual_json.name}: {exc}")
        return {}
    if not isinstance(data, list):
        writeLog("warning", logger, f"[ManualScanner] {manual_json.name} no es una lista; se regenerará.")
        return {}
    return {entry.get("file"): entry for entry in data if entry.get("file")}


def processManualPapersScanner(input_dir: Path = None, output_dir: Path = None):
    """Escanea manual_papers/ y crea o actualiza manual_papers.json automáticamente."""
    writeLog("info", logger, "🚀 [START] Processing processManualPapersScanner")
    if input_dir is None or output_dir is None:
        input_dir, output_dir = inicioModulo("processManualPapersScanner")

    manual_dir = input_dir / "manual_papers"
    if not manual_dir.is_dir():
        writeLog("info", logger, f"[ManualScanner] No existe {manual_dir}. Skipping.")
        return None

    pdfs = sorted(manual_dir.glob("*.pdf"))
    if not pdfs:
        writeLog("info", logger, f"[ManualScanner] No hay PDFs en {manual_dir}. Skipping.")
        return None

    manual_json = manual_dir / "manual_papers.json"
    existing = _load_existing(manual_json)

    entries = []
    added = updated = unchanged = 0
    for pdf in pdfs:
        entry = dict(existing.get(pdf.name, {}))
        entry["file"] = pdf.name
        detected = extract_pdf_metadata(pdf)
        changed = _merge_entry(entry, detected)

        if pdf.name in existing:
            if changed:
                updated += 1
            else:
                unchanged += 1
        else:
            added += 1
            writeLog("info", logger, f"[ManualScanner] 🆕 Detectado PDF: {pdf.name} -> {detected['title'][:60]}")

        entries.append(entry)

    write_json(manual_json, entries)
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, "MANUAL PAPERS SCANNER COMPLETED")
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, f"  PDFs encontrados: {len(pdfs)}")
    writeLog("info", logger, f"  Añadidos nuevos:  {added}")
    writeLog("info", logger, f"  Actualizados:     {updated}")
    writeLog("info", logger, f"  Sin cambios:      {unchanged}")
    writeLog("info", logger, f"  Fichero:          {manual_json}")
    writeLog("info", logger, "✅ [END] processManualPapersScanner")

    return entries


if __name__ == "__main__":
    import argparse
    import os

    from sources.common.common import processControl
    from sources.common.utils import configLoader

    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", type=str, default="AERASOA")
    args = parser.parse_args()

    config = configLoader()
    environment = config.get_environment()
    env = {
        key: (value if "realPath" in key else os.path.join(environment["realPath"], value))
        for key, value in environment.items()
    }
    processControl.args = args
    processControl.defaults = config.get_defaults()
    processControl.env = env

    processManualPapersScanner()
