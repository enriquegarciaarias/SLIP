#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_to_vault.py (USO ÚNICO - Versión Local por Subject)

Migra los PDFs descargados previamente a la arquitectura de Vault local.
Todo queda encapsulado dentro de output/{subject}/
"""

import json
import hashlib
import shutil
from pathlib import Path
import re


from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo




def sha1(text):
    return hashlib.sha1(text.encode('utf-8', errors='ignore')).hexdigest()


def normalized_title(title):
    if not title: return ""
    title = title.lower()
    title = re.sub(r'[^a-z0-9\s]', '', title)
    return re.sub(r'\s+', ' ', title).strip()


def get_global_id(paper):
    if paper.get("global_id"): return paper["global_id"]
    doi = (paper.get("doi") or "").strip().lower()
    if doi: return f"doi:{doi}"
    title = normalized_title(paper.get("title", ""))
    if title: return f"title:{sha1(title)}"
    return None


def get_vault_filename(global_id):
    """Genera un nombre seguro basado en el global_id (ej: sha1 del DOI)."""
    return f"{sha1(global_id)}.pdf"


def processMigracionInventory():
    input_dir, output_dir = inicioModulo("processSearchMergeEngine")

    working_pdf_dir = output_dir / "pdfs"
    selected_file = output_dir / "selected_papers.json"

    # Rutas locales encapsuladas en el subject
    vault_dir = output_dir / "_pdf_vault"
    inventory_file = output_dir / "pdf_inventory.json"

    if not selected_file.exists():
        print(f"❌ Error: No encontrado {selected_file}");
        return
    if not working_pdf_dir.exists():
        print(f"❌ Error: No existe el directorio de PDFs {working_pdf_dir}");
        return

    with open(selected_file, "r", encoding="utf-8") as f:
        selected_papers = json.load(f)

    print("\n" + "=" * 60)
    print("📦 MIGRACIÓN A VAULT LOCAL (POR SUBJECT)")
    print("=" * 60)
    print(f"Origen PDFs: {working_pdf_dir}")
    print(f"Vault:       {vault_dir}")
    print(f"Inventario:  {inventory_file}")
    print("-" * 60)

    vault_dir.mkdir(parents=True, exist_ok=True)

    inventory = {}
    if inventory_file.exists():
        with open(inventory_file, "r", encoding="utf-8") as f:
            inventory = json.load(f)
        print(f"ℹ️  Inventario pre-existente cargado ({len(inventory)} registros).")

    moved = 0
    skipped = 0
    missing = 0
    no_id = 0

    for paper in selected_papers:
        paper_id = paper.get("paper_id")
        global_id = get_global_id(paper)

        if not global_id:
            print(f"  ⚠️  [{paper_id}] Sin global_id. Ignorado.")
            no_id += 1;
            continue

        if global_id in inventory:
            skipped += 1;
            continue

        source_file = working_pdf_dir / f"{paper_id}.pdf"
        if not source_file.exists():
            missing += 1;
            continue

        vault_filename = get_vault_filename(global_id)
        dest_file = vault_dir / vault_filename

        try:
            shutil.move(str(source_file), str(dest_file))
            inventory[global_id] = vault_filename
            moved += 1

            short_gid = global_id[:35] + "..." if len(global_id) > 35 else global_id
            print(f"  ✅ [{moved}] {paper_id}.pdf -> {vault_filename} ({short_gid})")
        except Exception as e:
            print(f"  ❌ Error moviendo {paper_id}.pdf: {e}")

    with open(inventory_file, "w", encoding="utf-8") as f:
        json.dump(inventory, f, indent=2, ensure_ascii=False)

    print("-" * 60)
    print("📊 RESUMEN:")
    print(f"  Movidos al Vault:       {moved}")
    print(f"  Ya existían (omitidos): {skipped}")
    print(f"  PDFs faltantes:         {missing}")
    print(f"  Sin ID válido:          {no_id}")
    print(f"  TOTAL EN VAULT LOCAL:   {len(inventory)}")
    print(f"\n💾 Guardado en: {inventory_file}")
    print("\n✅ Migración completada. El directorio 'pdfs/' ahora está vacío.")
    print("    Ejecuta el nuevo processAcquisitionEngine para restaurarlos.\n")


if __name__ == "__main__":
    processMigracionInventory()