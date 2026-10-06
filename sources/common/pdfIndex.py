# pdfIndex.py
"""
Índice determinista paper_id -> PDF.

Durante la adquisición se persiste `pdf_download_log.json` con la
correspondencia autoritativa entre cada paper y su fichero PDF. La fase de
extracción de texto (`corpusCleaning`) consume ese índice directamente, de modo
que la asociación no depende del nombre del fichero ni del título del paper.

Formato de `pdf_download_log.json`:

    {
      "generated_at": "YYYY-MM-DD HH:MM:SS",
      "index":   { "wos_1": "pdfs/wos_1.pdf", ... },
      "entries": [ { "paper_id": "wos_1", "pdf_file": "pdfs/wos_1.pdf", "type": "auto" }, ... ]
    }

El lector admite también el formato legado (lista de eventos) para no romper
ejecuciones antiguas.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union

from sources.common.common import logger, writeLog

PDF_LOG_FILE = "pdf_download_log.json"


def _relative_to(path: Path, base: Path) -> str:
    """Devuelve `path` relativo a `base` (POSIX) o absoluto si no es posible."""
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def register_pdf(
    index: Dict[str, str],
    entries: List[Dict],
    paper_id: str,
    pdf_path: Union[str, Path],
    kind: str,
    output_dir: Union[str, Path],
) -> str:
    """
    Registra un PDF adquirido en el índice y en el log de eventos.

    Returns:
        Ruta relativa al directorio de salida (o absoluta si no es reducible).
    """
    output_dir = Path(output_dir)
    rel = _relative_to(Path(pdf_path), output_dir)
    index[paper_id] = rel
    entries.append({"paper_id": paper_id, "pdf_file": rel, "type": kind})
    return rel


def write_pdf_log(
    output_dir: Union[str, Path],
    index: Dict[str, str],
    entries: List[Dict],
) -> Path:
    """Persiste `pdf_download_log.json` con el índice y sus eventos."""
    output_dir = Path(output_dir)
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "index": index,
        "entries": entries,
    }
    out_file = output_dir / PDF_LOG_FILE
    with out_file.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return out_file


def load_pdf_index(output_dir: Union[str, Path]) -> Dict[str, Path]:
    """
    Carga el índice paper_id -> Path desde `pdf_download_log.json`.

    Admite el formato nuevo (dict con clave "index") y el legado (lista de
    eventos con `paper_id`/`pdf_file`). Devuelve siempre rutas resueltas.
    """
    output_dir = Path(output_dir)
    log_file = output_dir / PDF_LOG_FILE
    if not log_file.exists():
        return {}

    try:
        with log_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:  # noqa: BLE001 - fichero de estado corrupto
        writeLog("warning", logger, f"[pdfIndex] No se pudo leer {log_file}: {exc}")
        return {}

    raw: Dict[str, str] = {}
    if isinstance(data, dict):
        raw = data.get("index") or {}
    elif isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("paper_id") and entry.get("pdf_file"):
                raw[entry["paper_id"]] = entry["pdf_file"]

    resolved: Dict[str, Path] = {}
    for paper_id, pdf_file in raw.items():
        path = Path(pdf_file)
        resolved[paper_id] = path if path.is_absolute() else output_dir / path
    return resolved


def resolve_paper_pdf(
    paper: Dict,
    output_dir: Union[str, Path],
    index: Optional[Dict[str, Path]] = None,
) -> Optional[Path]:
    """
    Resuelve el PDF de un paper: primero el índice, después `local_pdf_path`.

    Returns:
        Path del PDF candidato (puede no existir) o None si no hay referencia.
    """
    output_dir = Path(output_dir)
    if index is None:
        index = load_pdf_index(output_dir)

    paper_id = paper.get("paper_id")
    if paper_id and paper_id in index:
        return index[paper_id]

    local = paper.get("local_pdf_path")
    if local:
        path = Path(local)
        return path if path.is_absolute() else output_dir / path
    return None
