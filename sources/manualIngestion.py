# manualIngestion.py
"""
Módulo Puente de Ingestión Manual (SOA).
Responsabilidad: Tomar PDFs externos, asignarles un ID de pipeline,
calcular su global_id y fusionarlos en el selected_papers.json existente.

Flujo de trabajo propuesto para el usuario:

El usuario deja los PDFs en: input/{subject}/manual_papers/
El usuario crea un JSON muy sencillo ahí mismo llamado manual_papers.json:

[
  {
    "file": "mi_paper_semillla.pdf",
    "title": "Designing for Human Centered AI Lessons Learned",
    "doi": "10.1016/j.ejor.2025.123456"
  },
  {
    "file": "otro_paper.pdf",
    "title": "A review on physiological signal processing"
  }
]
"""

from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo, sha1, normalized_title

import json
import re
import shutil
from pathlib import Path


def get_global_id(paper):
    """Igual que en SearchMerge y Acquisition."""
    doi = (paper.get("doi") or "").strip().lower()
    if doi: return f"doi:{doi}"
    title = normalized_title(paper.get("title", ""))
    if title: return f"title:{sha1(title)}"
    return None


def processManualIngestion():
    writeLog("info", logger, "🚀 [START] Processing processManualIngestion")
    input_dir, output_dir = inicioModulo("processManualIngestion")

    manual_dir = input_dir / "manual_papers"
    manual_json = manual_dir / "manual_papers.json"
    selected_file = output_dir / "selected_papers.json"

    # 1. Verificar que existe el archivo de definición manual
    if not manual_json.exists():
        writeLog("info", logger, "[Ingestion] No manual_papers.json found. Skipping.")
        return None

    with open(manual_json, "r", encoding="utf-8") as f:
        manual_entries = json.load(f)

    if not manual_entries:
        writeLog("info", logger, "[Ingestion] manual_papers.json is empty. Skipping.")
        return None

    # 2. Cargar los papers ya seleccionados por el pipeline automático
    if not selected_file.exists():
        writeLog("error", logger, f"[Ingestion] Error: {selected_file} not found. Run enrichmentEngine first.")
        return None

    with open(selected_file, "r", encoding="utf-8") as f:
        selected_papers = json.load(f)

    # Crear un set de global_ids ya existentes para evitar duplicados exactos
    existing_global_ids = {get_global_id(p) for p in selected_papers if get_global_id(p)}

    # Calcular el siguiente ID secuencial manual disponible (ej. manual_1, manual_2...)
    max_manual_idx = 0
    for p in selected_papers:
        pid = p.get("paper_id", "")
        match = re.match(r"manual_(\d+)", pid)
        if match:
            max_manual_idx = max(max_manual_idx, int(match.group(1)))

    ingested_count = 0
    skipped_duplicates = 0
    missing_files = 0

    # 3. Procesar cada entrada manual
    for entry in manual_entries:
        filename = entry.get("file", "")
        if not filename:
            continue

        source_pdf = manual_dir / filename
        if not source_pdf.exists():
            writeLog("warning", logger, f"[Ingestion] File not found: {filename}")
            missing_files += 1
            continue

        # Construir el paper al estilo del pipeline
        max_manual_idx += 1
        paper_id = f"manual_{max_manual_idx}"
        new_paper = {
            "paper_id": paper_id,
            "source": "manual_ingestion",
            "origin": entry.get("origin", "manual"),
            "doi": (entry.get("doi") or "").strip().lower() if entry.get("doi") else None,
            "title": entry.get("title", Path(filename).stem),
            "abstract": entry.get("abstract", ""),
            "year": entry.get("year"),
            "authors": entry.get("authors", []),
            "keywords": entry.get("keywords", []),
            "relevance_score": 1.0,  # Máxima relevancia porque el usuario lo eligió a mano
            "selected": True
        }
        if entry.get("bibkey"):
            new_paper["bibkey"] = entry["bibkey"]

        new_global_id = get_global_id(new_paper)
        if new_global_id:
            new_paper["global_id"] = new_global_id

        # Detección de duplicados mediante global_id
        if new_global_id in existing_global_ids:
            writeLog("info", logger,
                     f"[Ingestion] ⚠️ DUPLICADO: '{new_paper['title'][:50]}' ya existe en el pipeline. Omitido.")
            skipped_duplicates += 1
            continue

        # Copiar el PDF local a la carpeta de trabajo para que la adquisición
        # no intente descargarlo de nuevo (y quede archivado para el corpus).
        pdfs_dir = output_dir / "pdfs"
        pdfs_dir.mkdir(parents=True, exist_ok=True)
        target_pdf = pdfs_dir / f"{paper_id}.pdf"
        try:
            shutil.copy2(source_pdf, target_pdf)
            new_paper["local_pdf_path"] = str(target_pdf)
            new_paper["acquisition_status"] = "manual_local"
            new_paper["pdf_source"] = "manual_input"
        except Exception as exc:
            writeLog("warning", logger,
                     f"[Ingestion] No se pudo copiar {filename} a {target_pdf}: {exc}")

        # Añadir al pipeline
        existing_global_ids.add(new_global_id)
        selected_papers.append(new_paper)
        ingested_count += 1
        writeLog("info", logger,
                 f"[Ingestion] ✅ Inyectado: {new_paper['paper_id']} "
                 f"[{new_paper.get('origin', 'manual')}] -> {new_paper['title'][:50]}")

    # 4. Guardar el selected_papers.json actualizado (el contrato para Acquisition)
    if ingested_count > 0:
        with open(selected_file, "w", encoding="utf-8") as f:
            json.dump(selected_papers, f, indent=2, ensure_ascii=False)
        writeLog("info", logger, f"[Ingestion] {ingested_count} papers añadidos a {selected_file.name}")
    else:
        writeLog("info", logger, "[Ingestion] No new papers added to the pipeline.")

    # Resumen
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, "MANUAL INGESTION COMPLETED")
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, f"  Leídos del JSON: {len(manual_entries)}")
    writeLog("info", logger, f"  Inyectados con éxito: {ingested_count}")
    writeLog("info", logger, f"  Duplicados omitidos: {skipped_duplicates}")
    writeLog("info", logger, f"  PDFs no encontrados: {missing_files}")
    writeLog("info", logger, "✅ [END] processManualIngestion")

    return ingested_count


if __name__ == "__main__":
    processManualIngestion()