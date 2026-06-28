# ExistsOverview.py
"""
Generación de resumen de aproximaciones metodológicas en EXIST 2026
a partir de los PDFs de los working notes.
"""

from sources.common.common import logger, writeLog
from sources.common.fullTextExtractionEngine import extract_pdf_text
from sources.common.utils import inicioModulo
from sources.common.bibtex import extract_bibtex_from_pdf, extract_bib_key
from sources.common.llm_client import LLMClient
from pydantic import BaseModel, Field, validator
from typing import List
import os
import json
import re
from pathlib import Path
from tqdm import tqdm

# ========== CONFIGURACIÓN ==========
# Cambia a "llama3.2:3b" o "mistral" si Qwen sigue fallando
LLM_MODEL = "qwen3:8b"  # Prueba con "llama3.2:3b" si persiste el error
LLM_MODEL = "llama3.2:3b"

# ===================================

# ---------- Modelo Pydantic para equipo ----------
class TeamInfo(BaseModel):
    team_name: str = Field(..., description="Nombre del equipo o autores principales")
    tasks: List[int] = Field(default_factory=list, description="Lista de tareas (1,2,3)")
    models: List[str] = Field(default_factory=list, description="Modelos utilizados")
    techniques: str = Field(default="", description="Descripción de técnicas")
    results: str = Field(default="no especificado", description="Resultados mencionados")
    key_points: List[str] = Field(default_factory=list, description="Puntos clave de la metodología")

    @validator('tasks', each_item=True)
    def check_task_range(cls, v):
        if v not in [1, 2, 3]:
            raise ValueError(f"Task {v} must be 1, 2, or 3")
        return v


# ---------- Cliente LLM ----------
llm_client = LLMClient(model=LLM_MODEL, max_retries=2, default_temperature=0.0, default_max_tokens=1200)


# ---------- Extracción de información del equipo ----------
def extract_team_info(pdf_text: str, filename: str) -> TeamInfo:
    """
    Extrae información del equipo usando LLM y valida con Pydantic.
    Si falla, intenta construir TeamInfo con datos extraídos por regex.
    """
    system = """Eres un asistente que extrae información de artículos académicos.
    Debes devolver un objeto JSON con las siguientes claves EXACTAS:
    - "team_name": string (nombre del equipo o autores principales)
    - "tasks": lista de números (1,2,3) indicando las tareas
    - "models": lista de strings (modelos utilizados)
    - "techniques": string (descripción de técnicas)
    - "results": string (resultados mencionados, o "no especificado")
    - "key_points": lista de strings (2-3 puntos clave)

    IMPORTANTE: NO uses la clave "items". La respuesta debe ser un objeto JSON con las claves indicadas.
    Responde SOLO con el JSON, sin texto adicional, sin explicaciones, sin razonamiento.
    """

    prompt = f"""
    A continuación se presenta el texto de un artículo de los working notes de EXIST 2026.
    Extrae la información del equipo participante y devuélvela como un objeto JSON con las claves:
    team_name, tasks, models, techniques, results, key_points.

    EJEMPLO DE RESPUESTA CORRECTA:
    {{"team_name": "Jow", "tasks": [1,2,3], "models": ["XLM-RoBERTa", "CLIP"], "techniques": "Fusión multimodal con LeWiDi", "results": "Rank 4 en Task 2.3", "key_points": ["Etiquetas suaves", "Fusión de señales biométricas"]}}

    NO uses la clave "items". La respuesta debe comenzar con {{ y terminar con }}.

    Texto del artículo:
    {pdf_text[:8000]}
    """

    team_info = llm_client.generate_structured(prompt, TeamInfo, system_prompt=system,
                                               context=f"team_info for {filename}")
    if team_info:
        return team_info

    # Fallback: construir manualmente con extracción por regex
    writeLog("warning", logger, f"[extract_team_info] Validation failed for {filename}, using fallback extraction.")

    # Extraer team_name (mejorado)
    team_name = filename.replace(".pdf", "").replace("_", " ")
    team_patterns = [
        r'(?:Team|Group|Lab)\s*[:;]\s*([^\n.]+)',
        r'([A-Z][a-z]+(?: [A-Z][a-z]+)*)\s+(?:et al\.?|and colleagues)',
        r'^([A-Z][a-z]+(?: [A-Z][a-z]+)*)\s+at\s+(?:EXIST|CLEF)',
        r'([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)*)\s+[Aa]t\s+CLEF',
        r'^([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)\s+-\s+',
    ]
    for pattern in team_patterns:
        match = re.search(pattern, pdf_text[:500], re.IGNORECASE)
        if match:
            team_name = match.group(1).strip()
            break
    # Si no se encontró, buscar en el título
    if team_name == filename.replace(".pdf", "").replace("_", " "):
        lines = pdf_text.split('\n')
        for line in lines[:30]:
            line = line.strip()
            match = re.search(r'([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)\s+at\s+(?:EXIST|CLEF)', line)
            if match:
                team_name = match.group(1)
                break

    # Extraer tasks
    tasks = []
    for t in [1, 2, 3]:
        if re.search(rf'\b(Task|Subtask)\s*{t}\b', pdf_text[:2000], re.IGNORECASE):
            tasks.append(t)
        elif t == 1 and re.search(r'\b(tweets?|text)\b', pdf_text[:1000], re.IGNORECASE):
            tasks.append(t)
        elif t == 2 and re.search(r'\b(memes?|images?)\b', pdf_text[:1000], re.IGNORECASE):
            tasks.append(t)
        elif t == 3 and re.search(r'\b(videos?|TikTok)\b', pdf_text[:1000], re.IGNORECASE):
            tasks.append(t)

    # Extraer models
    common_models = ['BERT', 'RoBERTa', 'XLM-R', 'XLM-RoBERTa', 'CLIP', 'ViT', 'VideoMAE',
                     'Qwen', 'Llama', 'Gemma', 'SigLIP', 'Whisper', 'wav2vec', 'DeBERTa',
                     'DistilBERT', 'GPT', 'T5', 'mT5', 'BLIP', 'BLIP-2']
    models = []
    for model in common_models:
        if re.search(rf'\b{re.escape(model)}\b', pdf_text[:3000], re.IGNORECASE):
            models.append(model)
    if not models:
        models = ["modelos diversos"]

    # Extraer techniques
    techniques = "No especificado"
    method_section = re.search(
        r'(?i)(methodology|approach|system description|proposed method)[^\n]*\n(.*?)(?=\n\s*\n|\n(?=[A-Z]))', pdf_text,
        re.DOTALL)
    if method_section:
        techniques = method_section.group(2).strip()[:300]
    else:
        sentences = re.split(r'[.!?]+', pdf_text[:3000])
        for sent in sentences:
            if re.search(r'\b(approach|method|methodology|technique|architecture|pipeline)\b', sent,
                         re.IGNORECASE) and len(sent) > 30:
                techniques = sent.strip()[:200]
                break

    # Extraer results
    results = "no especificado"
    results_pattern = r'(F1[- ]?score|accuracy|rank|ICM)[^.]*?(\d+\.?\d*|top \d+|\d+º|\d+th)'
    match = re.search(results_pattern, pdf_text[:3000], re.IGNORECASE)
    if match:
        results = match.group(0).strip()

    # Extraer key_points
    key_points = []
    key_sentences = re.findall(r'(?:Our|We|The system|Our approach|Our method)[^.!?]*[.!?]', pdf_text[:2000])
    if key_sentences:
        key_points = [s.strip() for s in key_sentences[:3]]
    if not key_points:
        key_points = ["Metodología multimodal", "Aprendizaje con etiquetas suaves" if "soft" in pdf_text[
            :1000].lower() else "Enfoque basado en transformadores"]

    # Crear TeamInfo con los datos extraídos
    try:
        return TeamInfo(team_name=team_name, tasks=tasks, models=models, techniques=techniques, results=results,
                        key_points=key_points)
    except Exception as e:
        writeLog("error", logger, f"[extract_team_info] Could not create TeamInfo for {filename}: {e}")
        return TeamInfo(team_name=team_name, tasks=[])


# ---------- Generación de párrafo resumen ----------
def generate_summary_paragraph(info: TeamInfo, ref_id: int, bib_key: str = None) -> str:
    """Genera párrafo resumen usando LLMClient, con datos estructurados."""
    team = info.team_name
    tasks_str = ", ".join(str(t) for t in info.tasks) if info.tasks else "no especificadas"
    models = ", ".join(info.models) if info.models else "modelos diversos"
    techniques = info.techniques
    results = info.results
    key_points = ", ".join(info.key_points) if info.key_points else "no especificados"

    ref_marker = f"\\cite{{{bib_key}}}" if bib_key else f"[ref{ref_id}]"

    prompt = f"""
    Redacta un párrafo breve (3-4 líneas) que resuma la aproximación metodológica del equipo {team} en EXIST 2026.
    El equipo participó en las tareas {tasks_str}. Utilizó los modelos: {models}. 
    Técnicas destacadas: {techniques}. 
    Puntos clave: {key_points}.
    Resultados mencionados: {results}.
    El párrafo debe comenzar con '{team} {ref_marker}' y debe tener un estilo objetivo, similar a los resúmenes de los working notes de años anteriores.
    No incluyas información extra que no esté en los datos proporcionados.
    IMPORTANTE: Responde SOLO con el párrafo de resumen, sin introducciones ni comentarios adicionales.
    """
    system = "Eres un redactor académico que escribe resúmenes concisos y objetivos sobre metodologías de detección de sexismo."
    paragraph = llm_client.generate_text(prompt, system_prompt=system, context=f"summary for {team}")

    if paragraph and len(paragraph) > 10:
        return paragraph
    else:
        return f"{team} {ref_marker} (resumen no disponible)."


# ---------- Construcción del documento final ----------
def build_final_document(teams_info, bib_key_map=None) -> str:
    if bib_key_map is None:
        bib_key_map = {}

    # Clasificación
    only_task1, only_task2, only_task3, all_tasks = [], [], [], []
    for info, filename, ref_id in teams_info:
        tasks = set(info.tasks)
        if tasks == {1}:
            only_task1.append((info, ref_id))
        elif tasks == {2}:
            only_task2.append((info, ref_id))
        elif tasks == {3}:
            only_task3.append((info, ref_id))
        elif tasks == {1, 2, 3}:
            all_tasks.append((info, ref_id))
        else:
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

    for group, title in [(only_task1, "Task 1 (tweets)"), (only_task2, "Task 2 (memes)"),
                         (only_task3, "Task 3 (TikTok)"), (all_tasks, "tres tareas")]:
        if group:
            doc.append(f"### Equipos que participaron en {title}\n")
            for info, ref_id in group:
                bib_key = bib_key_map.get(ref_id)
                doc.append(generate_summary_paragraph(info, ref_id, bib_key) + "\n")

    doc.append("\n## Referencias\n")
    for idx, (info, filename, ref_id) in enumerate(teams_info, start=1):
        bib_key = bib_key_map.get(ref_id)
        team_name = info.team_name
        if bib_key:
            doc.append(f"[{idx}] {team_name} \\cite{{{bib_key}}}\n")
        else:
            doc.append(f"[{idx}] {team_name}. \"{filename}\", en Working Notes of CLEF 2026.\n")

    return "\n".join(doc)


# ---------- Funciones para LaTeX y BibTeX ----------
def escape_latex(text: str) -> str:
    replacements = {
        '\\': '\\textbackslash{}',
        '&': '\\&',
        '%': '\\%',
        '$': '\\$',
        '#': '\\#',
        '_': '\\_',
        '{': '\\{',
        '}': '\\}',
        '~': '\\textasciitilde{}',
        '^': '\\textasciicircum{}',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def generate_latex_document(summary_text: str, bib_entries: list, bib_keys: dict) -> str:
    lines = summary_text.split('\n')
    latex_lines = []
    for line in lines:
        if line.startswith('## '):
            latex_lines.append(f"\\section{{{escape_latex(line[3:].strip())}}}")
        elif line.startswith('### '):
            latex_lines.append(f"\\subsection{{{escape_latex(line[4:].strip())}}}")
        elif line.strip() == '':
            latex_lines.append('')
        else:
            latex_lines.append(escape_latex(line))
    body = '\n'.join(latex_lines)

    preamble = r"""\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage[spanish]{babel}
\usepackage{geometry}
\geometry{a4paper, margin=2.5cm}
\usepackage{parskip}
\usepackage{url}
\usepackage{natbib}
\bibliographystyle{plainnat}
\title{Resumen de aproximaciones metodológicas en EXIST 2026}
\author{Generado automáticamente}
\date{\today}

\begin{document}
\maketitle
"""
    bibliography = r"""
\bibliography{references}
\end{document}
"""
    return preamble + '\n' + body + '\n' + bibliography


def write_bib_file(bib_entries: list, output_path: Path):
    with open(output_path, 'w', encoding='utf-8') as f:
        for entry in bib_entries:
            if entry.get('bibtex'):
                f.write(entry['bibtex'] + '\n\n')


# ---------- Función principal ----------
def processExistsOverview():
    input_dir, output_dir = inicioModulo("processExistsOverview")
    pdfDir = input_dir / "pdfs_exist2026"
    if not os.path.exists(pdfDir):
        writeLog("error", logger, f"El directorio {pdfDir} no existe.")
        return

    pdf_files = [f for f in os.listdir(pdfDir) if f.lower().endswith('.pdf')]
    if not pdf_files:
        writeLog("error", logger, "No se encontraron archivos PDF en el directorio.")
        return

    teams_info = []
    bibtex_entries = []
    ref_counter = 1
    bib_key_map = {}

    for filename in tqdm(pdf_files, desc="Procesando PDFs"):
        pdf_path = os.path.join(pdfDir, filename)
        full_text = extract_pdf_text(pdf_path)
        if not full_text:
            writeLog("warning", logger, f"No se pudo extraer texto de {filename}. Se omite.")
            continue

        info = extract_team_info(full_text, filename)

        bib_data = extract_bibtex_from_pdf(pdf_path, full_text)
        bib_key = None
        if bib_data and isinstance(bib_data, dict):
            bibtex_str = bib_data.get("bibtex", "")
            if bibtex_str:
                bib_key = extract_bib_key(bibtex_str)
            bibtex_entries.append({
                "id": ref_counter,
                "filename": filename,
                "bibtex": bibtex_str,
                "metadata": bib_data.get("metadata", {})
            })
        else:
            bibtex_entries.append({
                "id": ref_counter,
                "filename": filename,
                "bibtex": None,
                "metadata": {}
            })

        if bib_key:
            bib_key_map[ref_counter] = bib_key

        teams_info.append((info, filename, ref_counter))
        ref_counter += 1

    if not teams_info:
        writeLog("error", logger, "No se pudo extraer información de ningún PDF.")
        return

    final_doc = build_final_document(teams_info, bib_key_map)

    output_txt = output_dir / "summary_exist2026.txt"
    with open(output_txt, 'w', encoding='utf-8') as f:
        f.write(final_doc)

    bib_file = output_dir / "references.bib"
    write_bib_file(bibtex_entries, bib_file)

    tex_content = generate_latex_document(final_doc, bibtex_entries, bib_key_map)
    tex_file = output_dir / "summary_exist2026.tex"
    with open(tex_file, 'w', encoding='utf-8') as f:
        f.write(tex_content)

    bibtex_json = output_dir / "bibtex.json"
    with open(bibtex_json, 'w', encoding='utf-8') as f:
        json.dump(bibtex_entries, f, indent=2, ensure_ascii=False)

    writeLog("info", logger, f"Documento de resumen (texto) generado en {output_txt}")
    writeLog("info", logger, f"Archivo BibTeX generado en {bib_file}")
    writeLog("info", logger, f"Documento LaTeX generado en {tex_file}")
    writeLog("info", logger, f"JSON con BibTeX generado en {bibtex_json}")


if __name__ == "__main__":
    processExistsOverview()