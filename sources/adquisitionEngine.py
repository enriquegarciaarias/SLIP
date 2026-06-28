# processAcquisitionEngine.py
from sources.common.common import logger, processControl, writeLog
from sources.common.pdf_resolver import resolve_and_download
from sources.common.utils import inicioModulo

import json
import re
import shutil
import urllib.parse
import hashlib
import os
import sys
import time
import select
from pathlib import Path


# ==================================================
# HELPERS DE IDENTIDAD (Duplicados de SearchMerge para calcular global_id)
# ==================================================
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
    return f"{sha1(global_id)}.pdf"


# ==================================================
# LÓGICA DE INTERACCIÓN HUMANA (Sin cambios, es perfecta)
# ==================================================
def build_google_scholar_url(paper: dict) -> str:
    title = paper.get("title", "")
    doi = paper.get("doi", "")
    query_parts = []
    if title: query_parts.append(f"Título: {title}")
    if doi: query_parts.append(f"DOI: {doi}")
    query = " ".join(query_parts)
    encoded_query = urllib.parse.quote(query)
    return f"https://scholar.google.com/scholar?hl=en&as_sdt=0%2C5&q={encoded_query}&btnG="


def get_nonblocking_input(timeout=0.5):
    rlist, _, _ = select.select([sys.stdin], [], [], timeout)
    if rlist:
        ch = sys.stdin.read(1).lower()
        if ch in ('n', 'f', 'y'): return ch
    return None


def manual_pdf_intervention(paper, pdf_dir, idx, total):
    manual_dir = pdf_dir / "manual"
    manual_dir.mkdir(parents=True, exist_ok=True)
    paper_id = paper.get("paper_id", f"unknown_{idx}")

    existing_pdfs = list(manual_dir.glob("*.pdf"))
    if existing_pdfs:
        print("\n" + "=" * 70)
        writeLog("warning", logger, f"📄 PAPELERA DE RECUPERACIÓN MANUAL [{idx}/{total}]")
        print("=" * 70)
        print(f"\nPaper ID: {paper_id}")
        print(f"Título: {paper.get('title', 'Sin título')}")
        print(f"\n📄 Se ha(n) detectado PDF(s) en la carpeta {manual_dir}.")
        pdf_file = existing_pdfs[0]
        target_filename = f"{paper_id}.pdf"
        target_path = pdf_dir / target_filename
        counter = 1
        while target_path.exists():
            target_path = pdf_dir / f"{paper_id}_{counter}.pdf"
            counter += 1
        try:
            shutil.move(str(pdf_file), str(target_path))
            print(f"\n   ✅ PDF incorporado exitosamente: {target_path.name}")
            return True, str(target_path)
        except Exception as e:
            print(f"\n   ❌ Error al mover el archivo: {e}")
            return False, None

    print("\n" + "=" * 70)
    writeLog("warning", logger, f"📄 PAPELERA DE RECUPERACIÓN MANUAL [{idx}/{total}]")
    print("=" * 70)
    print(f"\nPaper ID: {paper_id}")
    print(f"Título: {paper.get('title', 'Sin título')}")
    if paper.get('doi'): print(f"DOI: {paper['doi']}")
    if paper.get('landing_page'): print(f"Landing page: {paper['landing_page']}")
    if paper.get('pdf_url'):
        print(f"PDF URL: {paper['pdf_url']}")
    else:
        print(f"🔍 Google Scholar: {build_google_scholar_url(paper)}")

    print("\n" + "-" * 70)
    print("📌 Opciones:\n   Y = Incorporar PDF manualmente\n   N = Omitir\n   F = Finalizar")
    print("\n⏳ Esperando 60s para detectar PDF en la carpeta manual automático...")

    initial_pdfs = set(manual_dir.glob("*.pdf"))
    timeout = 60
    start_time = time.time()
    response = None

    while time.time() - start_time < timeout:
        ch = get_nonblocking_input(0.5)
        if ch: response = ch; break
        current_pdfs = set(manual_dir.glob("*.pdf"))
        if current_pdfs - initial_pdfs: response = 'y'; break
        elapsed = int(time.time() - start_time)
        if elapsed % 10 == 0 and elapsed > 0: print(f"   Esperando... {elapsed}s", end='\r')
        time.sleep(0.1)
    print()

    if response == 'f':
        return "finish", None
    elif response == 'n':
        return False, None
    elif response == 'y':
        pdf_files = list(manual_dir.glob("*.pdf"))
        if pdf_files:
            pdf_file = pdf_files[0]
            target_path = pdf_dir / f"{paper_id}.pdf"
            counter = 1
            while target_path.exists(): target_path = pdf_dir / f"{paper_id}_{counter}.pdf"; counter += 1
            try:
                shutil.move(str(pdf_file), str(target_path))
                print(f"\n   ✅ PDF incorporado exitosamente: {target_path.name}")
                return True, str(target_path)
            except Exception as e:
                print(f"\n   ❌ Error: {e}")

    print("\n⏰ Tiempo de espera automática finalizado.")
    while True:
        response = input("\n¿Opción? (Y/N/F): ").strip().lower()
        if response == 'f':
            return "finish", None
        elif response == 'n':
            return False, None
        elif response == 'y':
            pdf_files = list(manual_dir.glob("*.pdf"))
            if not pdf_files: print("   ❌ No se encontró PDF."); continue
            pdf_file = pdf_files[0]
            target_path = pdf_dir / f"{paper_id}.pdf"
            counter = 1
            while target_path.exists(): target_path = pdf_dir / f"{paper_id}_{counter}.pdf"; counter += 1
            try:
                shutil.move(str(pdf_file), str(target_path))
                print(f"\n   ✅ PDF incorporado exitosamente: {target_path.name}")
                return True, str(target_path)
            except Exception as e:
                print(f"\n   ❌ Error: {e}"); continue
        else:
            print("   Opción no válida.")


# ==================================================
# ORQUESTADOR PRINCIPAL (Con lógica de Vault)
# ==================================================
def archive_to_vault(target_file: Path, global_id: str, vault_dir: Path, inventory: dict):
    """Mueve el archivo al vault, lo renombra con hash, y lo copia de vuelta al target."""
    vault_filename = get_vault_filename(global_id)
    vault_file = vault_dir / vault_filename

    # Mover al vault
    shutil.move(str(target_file), str(vault_file))
    # Copiar de vuelta al directorio de trabajo
    shutil.copy2(str(vault_file), str(target_file))
    # Actualizar inventario en memoria
    inventory[global_id] = vault_filename


def pdf_acquisition_engine(selected_papers, output_dir):
    pdf_dir = output_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    vault_dir = output_dir / "_pdf_vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    inventory_file = output_dir / "pdf_inventory.json"

    # Cargar inventario existente
    inventory = {}
    if inventory_file.exists():
        with open(inventory_file, "r", encoding="utf-8") as f:
            inventory = json.load(f)

    downloaded, missing, manually_recovered = [], [], []
    # Esta será la salida limpia para el siguiente módulo
    output_papers = []

    for idx, paper in enumerate(selected_papers, start=1):
        out_paper = paper.copy()
        paper_id = paper.get("paper_id", f"unknown_{idx}")
        target_filename = f"{paper_id}.pdf"
        target_file = pdf_dir / target_filename

        out_paper.setdefault("local_pdf_path", None)
        out_paper.setdefault("acquisition_status", "pending")

        writeLog("info", logger, f"[{idx}/{len(selected_papers)}] Evaluating: {paper_id}...")

        # -------------------------------------------------------------
        # 1. CACHE LOOKUP EN EL VAULT
        # -------------------------------------------------------------
        global_id = get_global_id(paper)
        vault_hit = global_id and global_id in inventory

        if vault_hit:
            vault_filename = inventory[global_id]
            vault_file = vault_dir / vault_filename

            if vault_file.exists():
                writeLog("info", logger, f"  🚀 VAULT HIT: Copiando desde {vault_filename}")
                shutil.copy2(str(vault_file), str(target_file))

                out_paper["local_pdf_path"] = str(target_file)
                out_paper["acquisition_status"] = "cached_copy"
                downloaded.append({"paper_id": paper_id, "type": "cached_copy"})
                output_papers.append(out_paper)
                continue
            else:
                writeLog("warning", logger,
                         f"  ⚠️ VAULT CORRUPT: Archivo {vault_filename} no encontrado. Se descargará de nuevo.")
                del inventory[global_id]  # Limpiar basura del inventario
                vault_hit = False

        # -------------------------------------------------------------
        # 2. DESCARGA NUEVA O INTERVENCIÓN
        # -------------------------------------------------------------
        success = resolve_and_download(paper, target_file)

        if success:
            # Vault Dance: Guardar en vault y dejar copia en trabajo
            current_global_id = global_id or get_global_id(paper)
            if current_global_id:
                archive_to_vault(target_file, current_global_id, vault_dir, inventory)

            out_paper["local_pdf_path"] = str(target_file)
            out_paper["acquisition_status"] = "auto"
            downloaded.append({"paper_id": paper_id, "type": "auto"})
            output_papers.append(out_paper)
            writeLog("info", logger, f"  ✅ Descargado y archivado: {target_filename}")
        else:
            # -------------------------------------------------------------
            # 3. INTERVENCIÓN HUMANA
            # -------------------------------------------------------------
            writeLog("warning", logger, f"  ❌ Automatización fallida. Iniciando recuperación manual...")
            recovered, file_path = manual_pdf_intervention(paper, pdf_dir, idx, len(selected_papers))

            if recovered == "finish":
                out_paper["acquisition_status"] = "missing"
                missing.append({"paper_id": paper_id, "title": paper.get("title")})
                output_papers.append(out_paper)

                # Marcar el resto como missing directamente
                for j in range(idx, len(selected_papers)):
                    rp = selected_papers[j].copy()
                    rp["acquisition_status"] = "missing"
                    rp["local_pdf_path"] = None
                    output_papers.append(rp)
                    missing.append({"paper_id": rp.get("paper_id"), "title": rp.get("title")})
                break

            elif recovered and file_path:
                recovered_path = Path(file_path)
                # Vault Dance para el manual
                current_global_id = global_id or get_global_id(paper)
                if current_global_id:
                    archive_to_vault(recovered_path, current_global_id, vault_dir, inventory)

                out_paper["local_pdf_path"] = str(recovered_path)
                out_paper["acquisition_status"] = "manual"
                manually_recovered.append({"paper_id": paper_id, "type": "manual"})
                output_papers.append(out_paper)
                writeLog("info", logger, f"  🔄 Recuperado manualmente y archivado.")
            else:
                out_paper["acquisition_status"] = "missing"
                missing.append({"paper_id": paper_id, "title": paper.get("title")})
                output_papers.append(out_paper)
            print()

    # Guardar el inventario actualizado al final de todo el proceso
    with open(inventory_file, "w", encoding="utf-8") as f:
        json.dump(inventory, f, indent=2, ensure_ascii=False)

    return downloaded, missing, manually_recovered, output_papers


def processAcquisitionEngine():
    input_dir, output_dir = inicioModulo("processAcquisitionEngine")

    selected_file = output_dir / "selected_papers.json"
    if not selected_file.exists():
        writeLog("error", logger, f"Not found: {selected_file}")
        return None

    with open(selected_file, "r", encoding="utf-8") as f:
        selected_papers = json.load(f)

    writeLog("info", logger, f"Total de papers en entrada: {len(selected_papers)}")

    # Procesar adquisición de PDFs
    downloaded, missing, manually_recovered, output_papers = pdf_acquisition_engine(selected_papers, output_dir)

    # -------------------------------------------------------------
    # GUARDAR ESTADO PARA EL SIGUIENTE MÓDULO
    # -------------------------------------------------------------
    acquired_file = output_dir / "selected_acquired.json"
    with open(acquired_file, "w", encoding="utf-8") as f:
        json.dump(output_papers, f, indent=2, ensure_ascii=False)
    writeLog("info", logger, f"✅ Estado del pipeline guardado en {acquired_file}")

    # Logs de auditoría (Opcionales para el humano)
    with open(output_dir / "pdf_download_log.json", "w", encoding="utf-8") as f:
        json.dump(downloaded, f, indent=2, ensure_ascii=False)
    with open(output_dir / "missing_pdfs.json", "w", encoding="utf-8") as f:
        json.dump(missing, f, indent=2, ensure_ascii=False)

    # Resumen final
    writeLog("info", logger, "\n" + "=" * 70)
    writeLog("info", logger, "PROCESO DE ADQUISICIÓN DE PDFs COMPLETADO")
    writeLog("info", logger, "=" * 70)
    writeLog("info", logger,
             f"  🚀 Copiados desde Vault (Cache Hit): {sum(1 for d in downloaded if d['type'] == 'cached_copy')}")
    writeLog("info", logger, f"  ✅ Descargados nuevos (Auto): {sum(1 for d in downloaded if d['type'] == 'auto')}")
    writeLog("info", logger, f"  🔄 Recuperados manualmente: {len(manually_recovered)}")
    writeLog("info", logger, f"  ❌ PDFs faltantes (final): {len(missing)}")
    writeLog("info", logger, "✅ [END] processAcquisitionEngine")

    return {
        "downloaded": downloaded,
        "manually_recovered": manually_recovered,
        "still_missing": missing,
        "total_pdfs": len(downloaded) + len(manually_recovered)
    }


if __name__ == "__main__":
    processAcquisitionEngine()