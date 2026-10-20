#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
copySelectedPapers.py
=====================

Copia los PDFs de los papers seleccionados (según selected_papers.json) a una
carpeta dedicada: results/output/<investigacion>/selected_papers/

Sigue el estándar de los demás módulos del pipeline: no recibe parámetros,
obtiene output_dir y subject a través de processControl e inicioModulo.

Uso dentro del pipeline (main.py):
    from sources.copySelectedPapers import processCopySelectedPapers
    processCopySelectedPapers()
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from sources.common.common import logger, writeLog, processControl
from sources.common.utils import inicioModulo


def processCopySelectedPapers() -> Optional[Path]:
    """
    Copia los PDFs de los papers seleccionados a la carpeta
    results/output/<subject>/selected_papers/.

    Comportamiento:
        - Lee output_dir/selected_papers.json para obtener los paper_id
          seleccionados.
        - Crea output_dir/selected_papers/ si no existe.
        - VACÍA la carpeta si ya existe (borra su contenido).
        - Copia output_dir/pdfs/<paper_id>.pdf para cada paper seleccionado
          que exista en output_dir/pdfs/.
        - Escribe un archivo selected_papers_list.json dentro de
          output_dir/selected_papers/ con la relación de papers copiados.

    Retorna:
        Path a la carpeta results/output/<subject>/selected_papers/
    """
    _, output_dir = inicioModulo("processCopySelectedPapers")
    subject = processControl.args.subject

    selected_file = output_dir / "selected_papers.json"
    pdfs_dir = output_dir / "pdfs"
    target_dir = output_dir / "selected_papers"

    if not selected_file.exists():
        writeLog("error", logger,
                 f"[CopySelectedPapers] No se encontró {selected_file}")
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
                 f"[CopySelectedPapers] Formato inesperado en {selected_file}")
        return target_dir

    # Crear / vaciar carpeta destino
    if target_dir.exists():
        # Borrar contenido de la carpeta
        for item in target_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        writeLog("info", logger,
                 f"[CopySelectedPapers] Carpeta vaciada: {target_dir}")
    else:
        target_dir.mkdir(parents=True, exist_ok=True)
        writeLog("info", logger,
                 f"[CopySelectedPapers] Carpeta creada: {target_dir}")

    # Copiar PDFs
    copied = []
    not_found = []
    for pid in paper_ids:
        src = pdfs_dir / f"{pid}.pdf"
        if src.exists():
            dst = target_dir / f"{pid}.pdf"
            shutil.copy2(src, dst)
            copied.append({"paper_id": pid, "pdf": f"{pid}.pdf"})
            writeLog("debug", logger,
                     f"[CopySelectedPapers] Copiado: {pid}.pdf")
        else:
            not_found.append(pid)
            writeLog("warning", logger,
                     f"[CopySelectedPapers] PDF no encontrado: {src}")

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
             f"[CopySelectedPapers] {len(copied)} PDFs copiados a "
             f"{target_dir} (solicitados: {len(paper_ids)}, no encontrados: "
             f"{len(not_found)})")

    return target_dir


if __name__ == "__main__":
    processCopySelectedPapers()
