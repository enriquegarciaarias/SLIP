#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
pdfVaultReconcile.py
====================

Reconcilia el vault de PDFs (`pdfs/_pdf_vault` + `pdf_inventory.json`) con el
corpus actual, de modo que los PDFs ya descargados sigan siendo reutilizables
cuando la query cambia.

Motivación:
  El vault guarda cada PDF como `sha1(global_id).pdf` e indexa
  `pdf_inventory.json = {global_id: vault_file}`. Si en el pasado un paper se
  archivó con un `global_id` incorrecto (p. ej. el bug de DOIs de PubMed, que
  usaba el DOI de la última referencia), el contenido queda "inaccesible": una
  query futura busca por el `global_id` correcto y no lo encuentra, forzando
  una re-descarga.

Qué hace:
  Para cada PDF de trabajo (`pdfs/{paper_id}.pdf`) que no esté ya bien indexado:
    1. Identifica el paper en `canonical.json` -> `global_id` correcto.
    2. Si el contenido ya está en el vault (bajo cualquier clave), **re-clava**
       el fichero del vault a `sha1(global_id).pdf` y limpia las claves obsoletas.
    3. Si el contenido no está en el vault, lo **archiva** con la clave correcta.
  Al terminar, `pdf_inventory.json` queda alineado con el corpus actual.

Idempotente y no destructivo: nunca borra contenido sin que exista bajo la clave
correcta.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from sources.common.common import logger, writeLog


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _file_hash(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def reconcile_pdf_vault(
    output_dir: Path,
    canonical: Optional[List[dict]] = None,
    dry_run: bool = False,
) -> Dict[str, List]:
    """
    Reconcilia el vault con el corpus. Devuelve un informe con lo actuado.
    """
    output_dir = Path(output_dir)
    pdf_dir = output_dir / "pdfs"
    vault_dir = output_dir / "_pdf_vault"
    inventory_file = output_dir / "pdf_inventory.json"

    if canonical is None:
        canon_path = output_dir / "canonical.json"
        canonical = json.load(open(canon_path, encoding="utf-8")) if canon_path.exists() else []
    gid_by_pid = {
        p.get("paper_id"): p.get("global_id")
        for p in canonical
        if p.get("paper_id") and p.get("global_id")
    }

    inventory: Dict[str, str] = {}
    if inventory_file.exists():
        inventory = json.load(open(inventory_file, encoding="utf-8"))

    # content_hash -> fichero(s) del vault con ese contenido
    content_to_vault: Dict[str, List[Path]] = defaultdict(list)
    for vault_file in vault_dir.glob("*.pdf"):
        content_to_vault[_file_hash(vault_file)].append(vault_file)

    # vault_filename -> claves del inventario que lo referencian
    vault_to_keys: Dict[str, List[str]] = defaultdict(list)
    for gid, vfile in inventory.items():
        vault_to_keys[Path(vfile).name].append(gid)

    report = {"rekeyed": [], "archived": [], "already_ok": [], "skipped": []}

    for root_pdf in sorted(pdf_dir.glob("*.pdf")):
        paper_id = root_pdf.stem
        global_id = gid_by_pid.get(paper_id)
        if not global_id:
            report["skipped"].append((paper_id, "sin global_id en canonical"))
            continue

        target_name = f"{_sha1(global_id)}.pdf"
        target = vault_dir / target_name

        # Caso 1: ya existe con la clave correcta.
        if target.exists():
            inventory[global_id] = target_name
            report["already_ok"].append(paper_id)
            continue

        # Caso 2: el contenido ya está en el vault bajo otra clave -> re-clavar.
        matches = [
            v for v in content_to_vault.get(_file_hash(root_pdf), [])
            if v.exists() and v.name != target_name
        ]
        if matches:
            old = matches[0]
            stale_keys = [k for k in vault_to_keys.get(old.name, []) if k != global_id]
            for key in stale_keys:
                inventory.pop(key, None)
            if not dry_run:
                old.rename(target)
            vault_to_keys[target_name] = [global_id]
            inventory[global_id] = target_name
            report["rekeyed"].append({
                "paper_id": paper_id,
                "global_id": global_id,
                "from": old.name,
                "to": target_name,
                "stale_keys_removed": stale_keys,
            })
            continue

        # Caso 3: no está en el vault -> archivarlo con la clave correcta.
        if not dry_run:
            shutil.copy2(root_pdf, target)
        inventory[global_id] = target_name
        content_to_vault[_file_hash(root_pdf)].append(target)
        report["archived"].append({"paper_id": paper_id, "global_id": global_id})

    if not dry_run:
        with open(inventory_file, "w", encoding="utf-8") as handle:
            json.dump(inventory, handle, indent=2, ensure_ascii=False)

    # Claves del inventario que ya no corresponden a ningún global_id vigente.
    valid_gids = set(gid_by_pid.values())
    stale_remaining = sorted(k for k in inventory if k not in valid_gids)

    report["stale_keys_remaining"] = stale_remaining
    report["n_inventory"] = len(inventory)
    return report


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Reconcilia el vault de PDFs con el corpus")
    parser.add_argument("output_dir", help="results/output/<subject>")
    parser.add_argument("--dry-run", action="store_true", help="solo informar, sin cambios")
    args = parser.parse_args()

    report = reconcile_pdf_vault(Path(args.output_dir), dry_run=args.dry_run)
    mode = "DRY-RUN" if args.dry_run else "APLICADO"
    print(f"[{mode}] re-keyed={len(report['rekeyed'])} archivados={len(report['archived'])} "
          f"ya_ok={len(report['already_ok'])} saltados={len(report['skipped'])}")
    print(f"inventory={report['n_inventory']} | claves obsoletas restantes="
          f"{len(report['stale_keys_remaining'])}")
    for item in report["rekeyed"][:10]:
        print(f"  re-key {item['paper_id']}: {item['from']} -> {item['to']} "
              f"(obsoletas: {item['stale_keys_removed']})")


if __name__ == "__main__":
    main()
