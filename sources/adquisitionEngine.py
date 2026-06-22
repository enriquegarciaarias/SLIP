# processAcquisitionEngine.py
from sources.common.common import logger, processControl, writeLog

import requests
import json
import re
import shutil
import urllib.parse
from pathlib import Path


def download_file(url, output_file):
    try:
        r = requests.get(
            url,
            timeout=60,
            stream=True,
            allow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        content_type = r.headers.get("Content-Type", "")

        if "pdf" not in content_type.lower():
            return False

        with open(output_file, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)

        if output_file.stat().st_size < 10000:
            writeLog("error", logger, f"{output_file} too small maybe corrupt")
            return False
        return True

    except Exception:
        return False


def build_google_scholar_url(paper: dict) -> str:
    """
    Construye un enlace a Google Scholar usando título y DOI.
    """
    title = paper.get("title", "")
    doi = paper.get("doi", "")

    query_parts = []
    if title:
        query_parts.append(f"Título: {title}")
    if doi:
        query_parts.append(f"DOI: {doi}")

    query = " ".join(query_parts)
    encoded_query = urllib.parse.quote(query)

    return f"https://scholar.google.com/scholar?hl=en&as_sdt=0%2C5&q={encoded_query}&btnG="

import os
import sys
import time
import shutil
import select
from pathlib import Path

def get_nonblocking_input(timeout=0.5):
    """
    Lee una tecla sin esperar Enter.
    Devuelve la tecla (como cadena en minúscula) si se pulsa dentro del timeout,
    o None en caso contrario.
    """
    rlist, _, _ = select.select([sys.stdin], [], [], timeout)
    if rlist:
        ch = sys.stdin.read(1).lower()
        if ch in ('n', 'f', 'y'):
            return ch
        else:
            return None
    return None

def manual_pdf_intervention(paper, pdf_dir, idx, total):
    """
    Intervención del usuario con detección automática de PDF en 'manual' durante 60 segundos.
    Si no se detecta PDF ni tecla, pasa a modo manual con input() tradicional.
    """
    manual_dir = pdf_dir / "manual"
    manual_dir.mkdir(parents=True, exist_ok=True)

    paper_id = paper.get("paper_id", f"unknown_{idx}")

    # --- 1) Si ya hay PDFs en manual, los procesa directamente (sin esperar) ---
    existing_pdfs = list(manual_dir.glob("*.pdf"))
    if existing_pdfs:
        print("\n" + "=" * 70)
        writeLog("warning", logger, f"📄 PAPELERA DE RECUPERACIÓN MANUAL [{idx}/{total}]")
        print("=" * 70)
        print(f"\nPaper ID: {paper_id}")
        print(f"Título: {paper.get('title', 'Sin título')}")
        print(f"\n📄 Se ha(n) detectado PDF(s) en la carpeta {manual_dir}.")
        print("   Se procesará automáticamente el primer archivo.")
        pdf_file = existing_pdfs[0]
        target_filename = f"{paper_id}.pdf"
        target_path = pdf_dir / target_filename
        counter = 1
        while target_path.exists():
            target_path = pdf_dir / f"{paper_id}_{counter}.pdf"
            counter += 1
        try:
            shutil.move(str(pdf_file), str(target_path))
            print(f"\n   ✅ PDF incorporado exitosamente:")
            print(f"      Origen: {pdf_file.name}")
            print(f"      Destino: {target_path.name}")
            return True, str(target_path)
        except Exception as e:
            print(f"\n   ❌ Error al mover el archivo: {e}")
            return False, None

    # --- 2) No hay PDF previo, entramos en modo espera automática (60 segundos) ---
    print("\n" + "=" * 70)
    writeLog("warning", logger, f"📄 PAPELERA DE RECUPERACIÓN MANUAL [{idx}/{total}]")
    print("=" * 70)
    print(f"\nPaper ID: {paper_id}")
    print(f"Título: {paper.get('title', 'Sin título')}")
    if paper.get('doi'):
        print(f"DOI: {paper['doi']}")
    if paper.get('landing_page'):
        print(f"Landing page: {paper['landing_page']}")
    if paper.get('pdf_url'):
        print(f"PDF URL: {paper['pdf_url']}")
    else:
        gs_url = build_google_scholar_url(paper)
        print(f"🔍 Google Scholar: {gs_url}")

    print("\n" + "-" * 70)
    print("📌 Opciones:")
    print("   Y = Incorporar PDF manualmente (colocar en carpeta manual)")
    print("   N = Omitir este paper (no se descargará)")
    print("   F = Finalizar (todos los restantes se marcan como omitidos)")
    print("\n⏳ El sistema esperará hasta 60 segundos para detectar automáticamente un PDF.")
    print(f"   Si colocas un PDF en la carpeta {manual_dir}, se detectará automáticamente.")
    print("   Puedes escribir N o F en cualquier momento (sin Enter).")
    print("   Si no ocurre nada, se te pedirá que decidas manualmente.")

    # Obtener los PDFs ya existentes antes de empezar
    initial_pdfs = set(manual_dir.glob("*.pdf"))
    timeout = 60
    start_time = time.time()
    response = None

    while time.time() - start_time < timeout:
        # Verificar teclas (no bloqueante)
        ch = get_nonblocking_input(0.5)
        if ch:
            response = ch
            break

        # Verificar nuevos PDFs
        current_pdfs = set(manual_dir.glob("*.pdf"))
        new_pdfs = current_pdfs - initial_pdfs
        if new_pdfs:
            response = 'y'
            break

        # Indicador opcional de espera (cada 10 segundos)
        elapsed = int(time.time() - start_time)
        if elapsed % 10 == 0 and elapsed > 0:
            print(f"   Esperando... {elapsed}s", end='\r')
        time.sleep(0.1)

    print()  # salto de línea

    # --- 3) Procesar respuesta de la fase automática ---
    if response == 'f':
        print("   ✗ Finalizando proceso. Los papers restantes se marcarán como omitidos.")
        return "finish", None
    elif response == 'n':
        print("   ✗ Incorporación manual omitida.")
        return False, None
    elif response == 'y':
        # Buscar el nuevo PDF (puede ser el que se detectó en el bucle)
        pdf_files = list(manual_dir.glob("*.pdf"))
        if not pdf_files:
            print("   ❌ No se encontró ningún PDF en la carpeta manual.")
            # Si no hay PDF (por ejemplo, se detectó pero luego se borró), pasamos a modo manual
            # Podríamos pasar a la fase manual aquí también.
            # Para simplificar, asumimos que el usuario quiere intentar de nuevo manualmente.
            pass
        else:
            pdf_file = pdf_files[0]
            target_filename = f"{paper_id}.pdf"
            target_path = pdf_dir / target_filename
            counter = 1
            while target_path.exists():
                target_path = pdf_dir / f"{paper_id}_{counter}.pdf"
                counter += 1
            try:
                shutil.move(str(pdf_file), str(target_path))
                print(f"\n   ✅ PDF incorporado exitosamente:")
                print(f"      Origen: {pdf_file.name}")
                print(f"      Destino: {target_path.name}")
                return True, str(target_path)
            except Exception as e:
                print(f"\n   ❌ Error al mover el archivo: {e}")
                # Si falla el movimiento, pasamos a modo manual para que el usuario decida
                pass

    # --- 4) Fase manual (input tradicional) ---
    # Llegamos aquí si:
    # - No hubo tecla ni PDF en 60s
    # - Hubo 'y' pero no se encontró PDF (raro)
    # - Hubo error al mover el PDF
    print("\n⏰ Tiempo de espera automática finalizado.")
    print(f"   Puedes colocar el PDF en la carpeta {manual_dir} y luego teclear Y.")
    print("   O teclear N para omitir, o F para finalizar.")

    # Ahora sí, usamos input() tradicional
    while True:
        response = input("\n¿Opción? (Y/N/F): ").strip().lower()
        if response == 'f':
            print("   ✗ Finalizando proceso. Los papers restantes se marcarán como omitidos.")
            return "finish", None
        elif response == 'n':
            print("   ✗ Incorporación manual omitida.")
            return False, None
        elif response == 'y':
            # Buscar PDF en manual
            pdf_files = list(manual_dir.glob("*.pdf"))
            if not pdf_files:
                print("   ❌ No se encontró ningún PDF en la carpeta manual.")
                print("   Por favor, coloca el PDF y teclea Y de nuevo, o N para omitir.")
                continue
            pdf_file = pdf_files[0]
            target_filename = f"{paper_id}.pdf"
            target_path = pdf_dir / target_filename
            counter = 1
            while target_path.exists():
                target_path = pdf_dir / f"{paper_id}_{counter}.pdf"
                counter += 1
            try:
                shutil.move(str(pdf_file), str(target_path))
                print(f"\n   ✅ PDF incorporado exitosamente:")
                print(f"      Origen: {pdf_file.name}")
                print(f"      Destino: {target_path.name}")
                return True, str(target_path)
            except Exception as e:
                print(f"\n   ❌ Error al mover el archivo: {e}")
                print("   Inténtalo de nuevo.")
                continue
        else:
            print("   Opción no válida. Por favor, introduce Y, N o F.")


def pdf_acquisition_engine(selected_papers, output_dir):
    pdf_dir = output_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    manual_dir = pdf_dir / "manual"
    manual_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    missing = []
    manually_recovered = []

    # Limpiar PDFs residuales en manual
    residual_pdfs = list(manual_dir.glob("*.pdf"))
    if residual_pdfs:
        writeLog("warning", logger, f"Se encontraron {len(residual_pdfs)} PDFs residuales en {manual_dir}")
        for pdf in residual_pdfs:
            writeLog("warning", logger, f"  - {pdf.name} (serán ignorados)")

    for idx, paper in enumerate(selected_papers, start=1):
        paper_id = paper.get("paper_id", f"unknown_{idx}")
        target_filename = f"{paper_id}.pdf"
        output_file = pdf_dir / target_filename
        pdf_url = paper.get("pdf_url")

        # Verificar si el PDF ya existe (de ejecución anterior)
        if output_file.exists():
            writeLog("info", logger, f"[{idx}/{len(selected_papers)}] ✅ Already exists: {target_filename}")
            downloaded.append({
                "paper_id": paper_id,
                "title": paper.get("title"),
                "file": str(output_file)
            })
            continue

        writeLog("info", logger,
                 f"[{idx}/{len(selected_papers)}] Processing: {paper_id} - {paper.get('title', '')[:50]}...")
        success = False

        if pdf_url and pdf_url != "None":
            success = download_file(pdf_url, output_file)

        if success:
            downloaded.append({
                "paper_id": paper_id,
                "title": paper.get("title"),
                "file": str(output_file)
            })
            writeLog("info", logger, f"  ✅ Descargado automáticamente: {target_filename}")
        else:
            if pdf_url:
                writeLog("warning", logger, f"  ❌ Failed to download: {pdf_url}")
            else:
                writeLog("info", logger, f"  ℹ️ No PDF URL available")

            recovered, file_path = manual_pdf_intervention(paper, pdf_dir, idx, len(selected_papers))

            if recovered == "finish":
                # Marcar todos los papers restantes como missing
                writeLog("info", logger, f"  🏁 Finalizando proceso. Marcando papers restantes como missing.")
                for j in range(idx, len(selected_papers)):
                    remaining_paper = selected_papers[j]
                    remaining_id = remaining_paper.get("paper_id", f"unknown_{j + 1}")
                    missing.append({
                        "paper_id": remaining_id,
                        "title": remaining_paper.get("title"),
                        "doi": remaining_paper.get("doi"),
                        "pdf_url": remaining_paper.get("pdf_url"),
                        "landing_page": remaining_paper.get("landing_page")
                    })
                break
            elif recovered and file_path:
                manually_recovered.append({
                    "paper_id": paper_id,
                    "title": paper.get("title"),
                    "file": file_path
                })
                writeLog("info", logger, f"  🔄 Recuperado manualmente: {Path(file_path).name}")
            else:
                missing.append({
                    "paper_id": paper_id,
                    "title": paper.get("title"),
                    "doi": paper.get("doi"),
                    "pdf_url": pdf_url,
                    "landing_page": paper.get("landing_page")
                })
                writeLog("info", logger, f"  💾 Marcado como missing: {paper_id}")

            print()

    return downloaded, missing, manually_recovered, pdf_dir


def processAcquisitionEngine():
    writeLog("info", logger, "🚀 [START] Processing processAcquisitionEngine")
    base_input_dir = Path(processControl.env.get("input", ""))
    base_output_dir = Path(processControl.env.get("output", ""))
    subject = processControl.args.subject
    input_dir = base_input_dir / subject
    output_dir = base_output_dir / subject

    selected_file = output_dir / "selected_papers.json"
    if not selected_file.exists():
        writeLog("error", logger, f"Not found: {selected_file}")
        return None

    with open(selected_file, "r", encoding="utf-8") as f:
        selected_papers = json.load(f)

    writeLog("info", logger, f"Total de papers seleccionados: {len(selected_papers)}")

    # Procesar adquisición de PDFs
    downloaded, missing, manually_recovered, pdf_dir = pdf_acquisition_engine(selected_papers, output_dir)

    # Guardar logs
    with open(output_dir / "pdf_download_log.json", "w", encoding="utf-8") as f:
        json.dump(downloaded, f, indent=2, ensure_ascii=False)

    with open(output_dir / "manual_recovery_log.json", "w", encoding="utf-8") as f:
        json.dump(manually_recovered, f, indent=2, ensure_ascii=False)

    with open(output_dir / "missing_pdfs.json", "w", encoding="utf-8") as f:
        json.dump(missing, f, indent=2, ensure_ascii=False)

    # Resumen final
    writeLog("info", logger, "\n" + "=" * 70)
    writeLog("info", logger, "PROCESO DE ADQUISICIÓN DE PDFs COMPLETADO")
    writeLog("info", logger, "=" * 70)
    writeLog("info", logger, f"  ✅ Descarga automática exitosa: {len(downloaded)}")
    writeLog("info", logger, f"  🔄 Recuperados manualmente: {len(manually_recovered)}")
    writeLog("info", logger, f"  ❌ Total de PDFs obtenidos: {len(downloaded) + len(manually_recovered)}")
    writeLog("info", logger, f"  ⚠️  PDFs faltantes (final): {len(missing)}")

    # Advertencia si quedan PDFs en manual sin procesar
    manual_dir = pdf_dir / "manual"
    if manual_dir.exists():
        leftover = list(manual_dir.glob("*.pdf"))
        if leftover:
            writeLog("warning", logger, f"\n  ⚠️ Quedaron {len(leftover)} PDFs sin procesar en {manual_dir}")
            writeLog("warning", logger, "     Estos PDFs no fueron asociados a ningún paper.")
            for pdf in leftover:
                writeLog("warning", logger, f"       - {pdf.name}")

    return {
        "downloaded": downloaded,
        "manually_recovered": manually_recovered,
        "still_missing": missing,
        "total_pdfs": len(downloaded) + len(manually_recovered)
    }


if __name__ == "__main__":
    processAcquisitionEngine()