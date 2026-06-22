from sources.common.common import logger, processControl, writeLog
from sources.fullTextExtractionEngine import extract_pdf_text, extract_sections
from sources.common.utils import inicioModulo

import os
import json
import re
from pathlib import Path
import ollama
from tqdm import tqdm

import sys

MODEL_NAME = "qwen3:8b"  # Ajusta según tu modelo Qwen

def extract_clean_text(pdf_path: str) -> str:
    """Usa tu función extract_pdf_text de sources.common.common."""
    return extract_pdf_text(pdf_path)


def query_ollama(prompt: str, system_prompt: str = "") -> str:
    """Envía una consulta a Ollama con el modelo Qwen."""
    try:
        response = ollama.chat(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ]
        )
        return response['message']['content'].strip()
    except Exception as e:
        print(f"Error en la consulta a Ollama: {e}")
        return ""


def extract_team_info(pdf_text: str, filename: str) -> dict:
    """
    Usa Qwen para extraer información estructurada del equipo a partir del texto del PDF.
    """
    system = "Eres un asistente que extrae información de artículos académicos sobre detección de sexismo en redes sociales. Devuelve únicamente un objeto JSON con las claves: team_name, tasks (lista de números: 1,2,3), models (lista de modelos usados), techniques (resumen de técnicas), results (si los menciona, o 'no especificado'), key_points (breve lista de aspectos relevantes)."

    prompt = f"""
    A continuación se presenta el texto de un artículo de los working notes de EXIST 2026. 
    Extrae la siguiente información y devuélvela en formato JSON (solo el JSON, sin texto adicional):
    - team_name: nombre del equipo o autores principales.
    - tasks: lista de las tareas en las que participó (1, 2, 3). Por ejemplo, [1,2] o [1,2,3].
    - models: lista de modelos utilizados (ej. XLM-RoBERTa, ViT, VideoMAE, etc.)
    - techniques: descripción concisa de las técnicas, preprocesamiento, fusión, estrategias de etiquetado, etc.
    - results: si se mencionan resultados, extrae la posición o métrica más relevante, sino pon "no especificado".
    - key_points: lista de 2-3 puntos clave de la metodología.

    Texto del artículo:
    {pdf_text[:8000]}  # Limitamos a 8000 caracteres para no sobrecargar el contexto
    """
    response = query_ollama(prompt, system)
    try:
        json_str = re.search(r'\{.*\}', response, re.DOTALL)
        if json_str:
            info = json.loads(json_str.group())
        else:
            info = {}
    except:
        info = {}
    # Asegurar campos obligatorios
    info.setdefault("team_name", filename.replace(".pdf", ""))
    info.setdefault("tasks", [])
    info.setdefault("models", [])
    info.setdefault("techniques", "")
    info.setdefault("results", "no especificado")
    info.setdefault("key_points", [])
    return info


def generate_summary_paragraph(info: dict, ref_id: int) -> str:
    """
    Usa Qwen para redactar un párrafo resumen del equipo, similar al estilo de 2025.
    """
    team = info.get("team_name", "Equipo desconocido")
    tasks = info.get("tasks", [])
    models = ", ".join(info.get("models", [])) if info.get("models") else "modelos diversos"
    techniques = info.get("techniques", "")
    results = info.get("results", "")
    key_points = info.get("key_points", [])

    prompt = f"""
    Redacta un párrafo breve (unas 3-4 líneas) que resuma la aproximación metodológica del equipo {team} en EXIST 2026.
    El equipo participó en las tareas {tasks}. Utilizó los modelos: {models}. 
    Técnicas destacadas: {techniques}. 
    Puntos clave: {', '.join(key_points)}.
    Resultados mencionados: {results}.
    El párrafo debe comenzar con '{team} [ref{ref_id}]' y debe tener un estilo objetivo, similar a los resúmenes de los working notes de años anteriores.
    No incluyas información extra que no esté en los datos proporcionados.
    """
    system = "Eres un redactor académico que escribe resúmenes concisos y objetivos sobre metodologías de detección de sexismo."
    paragraph = query_ollama(prompt, system)
    return paragraph


def build_final_document(teams_info) -> str:
    """
    Organiza los párrafos por tipo de tarea y genera el documento final.
    teams_info: lista de (info, filename, ref_id)
    """
    # Clasificar
    only_task1 = []
    only_task2 = []
    only_task3 = []
    all_tasks = []

    for info, filename, ref_id in teams_info:
        tasks = set(info.get("tasks", []))
        if tasks == {1}:
            only_task1.append((info, ref_id))
        elif tasks == {2}:
            only_task2.append((info, ref_id))
        elif tasks == {3}:
            only_task3.append((info, ref_id))
        elif tasks == {1, 2, 3}:
            all_tasks.append((info, ref_id))
        else:
            # Participó en combinaciones mixtas, por simplicidad lo dejamos en all_tasks si incluye las tres
            if 1 in tasks and 2 in tasks and 3 in tasks:
                all_tasks.append((info, ref_id))
            elif 1 in tasks and 2 in tasks:
                only_task1.append((info, ref_id))
            else:
                if 1 in tasks:
                    only_task1.append((info, ref_id))
                elif 2 in tasks:
                    only_task2.append((info, ref_id))
                elif 3 in tasks:
                    only_task3.append((info, ref_id))
                else:
                    all_tasks.append((info, ref_id))

    doc = []
    doc.append("## Resumen de las aproximaciones metodológicas de los equipos participantes en EXIST 2026\n")
    doc.append(
        "A continuación se resumen las estrategias seguidas por los equipos que presentaron artículo en los Working Notes.\n")

    if only_task1:
        doc.append("### Equipos que participaron únicamente en la Task 1 (procesamiento de tweets)\n")
        for info, ref_id in only_task1:
            para = generate_summary_paragraph(info, ref_id)
            doc.append(para + "\n")

    if only_task2:
        doc.append("### Equipos que participaron únicamente en la Task 2 (memes)\n")
        for info, ref_id in only_task2:
            para = generate_summary_paragraph(info, ref_id)
            doc.append(para + "\n")

    if only_task3:
        doc.append("### Equipos que participaron únicamente en la Task 3 (videos TikTok)\n")
        for info, ref_id in only_task3:
            para = generate_summary_paragraph(info, ref_id)
            doc.append(para + "\n")

    if all_tasks:
        doc.append("### Equipos que participaron en las tres tareas (tweets, memes y TikTok)\n")
        for info, ref_id in all_tasks:
            para = generate_summary_paragraph(info, ref_id)
            doc.append(para + "\n")

    doc.append("\n## Referencias\n")
    for idx, (info, filename, ref_id) in enumerate(teams_info, start=1):
        # Usamos el nombre del archivo como título provisional
        doc.append(f"[{ref_id}] {info.get('team_name', filename)}. \"{filename}\", en Working Notes of CLEF 2026.\n")

    return "\n".join(doc)


def processExistsOverview():
    input_dir, output_dir = inicioModulo("processExistsOverview")
    pdfDir = input_dir / "pdfs_exist2026"
    if not os.path.exists(pdfDir):
        print(f"El directorio {pdfDir} no existe.")
        return

    pdf_files = [f for f in os.listdir(pdfDir) if f.lower().endswith('.pdf')]
    if not pdf_files:
        print("No se encontraron archivos PDF en el directorio.")
        return

    teams_info = []
    ref_counter = 1

    for filename in tqdm(pdf_files, desc="Procesando PDFs"):
        pdf_path = os.path.join(pdfDir, filename)
        # Usamos tu función extract_pdf_text
        full_text = extract_pdf_text(pdf_path)
        if not full_text:
            print(f"Advertencia: no se pudo extraer texto de {filename}. Se omite.")
            continue

        # Opcional: también podemos extraer secciones con tu extract_sections, pero para el resumen basta con el texto completo.
        # Extraer información estructurada
        info = extract_team_info(full_text, filename)
        ref_id = ref_counter
        ref_counter += 1
        teams_info.append((info, filename, ref_id))

    if not teams_info:
        print("No se pudo extraer información de ningún PDF.")
        return

    final_doc = build_final_document(teams_info)
    output_file = output_dir / "summary_exist2026.txt"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(final_doc)

    print(f"Documento generado en {output_file}")


if __name__ == "__main__":
    processExistsOverview()