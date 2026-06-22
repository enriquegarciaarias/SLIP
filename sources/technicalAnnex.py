# generate_technical_annex.py
from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo

import json
import re
import time
import requests
from pathlib import Path

# --------------------------------------------------
# CONFIGURACIÓN (parametrizable aquí o en studyDescription.json)
# --------------------------------------------------
LLM_CONFIG = {
    "backend": "ollama",
    "model": "llama3.2:3b",
    "url": "http://localhost:11434/api/generate",
}
MAX_SECTION_CHARS = 3000
REQUEST_TIMEOUT = 90
SLEEP_BETWEEN_CALLS = 1  # segundos
# 🔥 Aumentamos el número de tokens para que el LLM pueda completar el JSON
LLM_NUM_PREDICT = 4096  # Antes 2048


# --------------------------------------------------
# FUNCIONES AUXILIARES
# --------------------------------------------------

def load_json(file_path: Path) -> dict:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_study_config(input_dir: Path) -> dict:
    """Carga la configuración del estudio (focus_terms, technical_extraction, etc.)"""
    config_file = input_dir / "studyDescription.json"
    if not config_file.exists():
        writeLog("error", logger, f"[TechAnnex] studyDescription.json not found in {input_dir}")
        return {}
    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("project", {})


def clean_and_fix_json(response: str) -> str:
    """
    Intenta limpiar y reparar un JSON truncado o mal formado.
    - Elimina texto antes de la primera llave.
    - Elimina texto después de la última llave.
    - Añade llaves de cierre si faltan.
    """
    # Buscar el primer '{'
    start = response.find('{')
    if start == -1:
        return response
    # Buscar el último '}' (podría estar truncado, así que buscamos el último que parezca completo)
    # Buscar la última llave de cierre que tenga una estructura razonable (que no esté dentro de una cadena)
    # Simplificamos: buscar el último '}' que no esté seguido de una coma o dentro de una cadena.
    # Usamos una búsqueda simple desde el final.
    end = response.rfind('}')
    if end == -1:
        # No hay llave de cierre, intentamos añadirla
        # Tomamos desde start hasta el final y añadimos '}'
        # Pero necesitamos asegurarnos de que no haya contenido extra después de start
        # Lo más seguro es truncar en la última coma antes de un cierre esperado.
        # Simple: tomamos completo desde start y añadimos '}'
        fixed = response[start:] + '}'
        return fixed
    else:
        # Tomamos desde start hasta end+1
        fixed = response[start:end+1]
        # Verificar si el JSON está completo
        try:
            json.loads(fixed)
            return fixed
        except json.JSONDecodeError:
            # Si no es válido, intentar añadir llaves de cierre faltantes
            # Contar llaves abiertas y cerradas
            open_braces = fixed.count('{')
            close_braces = fixed.count('}')
            if open_braces > close_braces:
                fixed += '}' * (open_braces - close_braces)
            return fixed


def call_llm(prompt: str, max_retries: int = 2) -> str:
    """Llama al LLM con reintentos y extrae JSON si está envuelto en markdown."""
    for attempt in range(max_retries):
        try:
            response = requests.post(
                LLM_CONFIG["url"],
                json={
                    "model": LLM_CONFIG["model"],
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": LLM_NUM_PREDICT,  # 🔥 Aumentado
                    },
                },
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                result = response.json().get("response", "").strip()
                if result:
                    # Intentar extraer JSON del bloque de código markdown
                    json_match = re.search(r'```json\s*(\{.*?\})\s*```', result, re.DOTALL)
                    if json_match:
                        candidate = json_match.group(1).strip()
                        # Intentar reparar
                        candidate = clean_and_fix_json(candidate)
                        return candidate
                    # Si no, intentar encontrar cualquier objeto JSON
                    json_match = re.search(r'\{.*?\}', result, re.DOTALL)
                    if json_match:
                        candidate = json_match.group(0).strip()
                        candidate = clean_and_fix_json(candidate)
                        return candidate
                    # Si no hay JSON, devolver la respuesta limpia
                    return clean_and_fix_json(result)
                else:
                    writeLog("warning", logger, f"LLM returned empty response (attempt {attempt + 1})")
            else:
                writeLog("warning", logger, f"LLM error {response.status_code} (attempt {attempt + 1})")
        except Exception as e:
            writeLog("warning", logger, f"LLM exception (attempt {attempt + 1}): {e}")
        time.sleep(2)
    return ""


def build_prompt(title: str, methodology: str, results: str, fields: list) -> str:
    """Construye el prompt dinámicamente a partir de la lista de campos."""
    # Crear la descripción de la estructura JSON
    fields_desc = []
    for field in fields:
        field_name = field["name"]
        field_desc = field.get("description", "")
        field_type = field.get("type", "string")
        if field_type == "list":
            fields_desc.append(f'  "{field_name}": ["list", "of", "{field_desc}"]')
        else:
            fields_desc.append(f'  "{field_name}": "string describing {field_desc}"')
    json_schema = "{\n" + ",\n".join(fields_desc) + "\n}"

    prompt = f"""You are an AI assistant extracting technical implementation details from academic papers.
Extract the following fields from the provided text and return ONLY a valid JSON object. Do not add any extra text.

**Title:** {title}

**Methodology / Materials & Methods:**
{methodology[:MAX_SECTION_CHARS]}

**Results / Experiments:**
{results[:MAX_SECTION_CHARS]}

**Required JSON structure:**
{json_schema}

Instructions:
- If a field is missing or cannot be inferred, use null for strings or [] for lists.
- Ensure the JSON is complete with all closing braces and brackets.
- Do not include any text outside the JSON object.
- Output the JSON object only.

JSON:"""
    return prompt


def extract_technical_profile(paper_dict: dict, paper_title: str, fields: list) -> dict:
    """
    Extrae el perfil técnico de un paper usando el LLM.
    Se enfoca en las secciones de metodología y resultados.
    """
    # Obtener secciones estructuradas
    sections = paper_dict.get("sections", {})
    methodology = sections.get("methodology", "") or sections.get("methods", "") or sections.get("approach", "")
    results = sections.get("results", "") or sections.get("experiments", "") or sections.get("evaluation", "")

    # Si no hay secciones, usar texto completo
    if not methodology and not results:
        full_text = paper_dict.get("full_text", "") or paper_dict.get("clean_text", "")
        methodology = full_text[:MAX_SECTION_CHARS]
        results = ""

    if not methodology:
        methodology = paper_dict.get("full_text", "")[:MAX_SECTION_CHARS] if isinstance(paper_dict, dict) else ""

    prompt = build_prompt(paper_title, methodology, results, fields)
    response = call_llm(prompt)

    if not response:
        writeLog("warning", logger, f"[TechAnnex] No response from LLM for {paper_title[:50]}...")
        return {}

    try:
        # Limpiar caracteres de control
        cleaned_response = re.sub(r'[\x00-\x1f]', '', response)
        data = json.loads(cleaned_response)
        # Asegurar que las listas sean listas
        for field in fields:
            if field["type"] == "list" and field["name"] in data:
                if not isinstance(data[field["name"]], list):
                    data[field["name"]] = []
        return data
    except json.JSONDecodeError as e:
        writeLog("warning", logger, f"[TechAnnex] JSON decode error: {e}")
        writeLog("warning", logger, f"Response snippet: {response[:300]}")
        # Intentar reparar: a veces el JSON está cortado por la mitad
        # Si termina sin cerrar, añadir llaves
        if response and response.count('{') > response.count('}'):
            response += '}' * (response.count('{') - response.count('}'))
            try:
                data = json.loads(response)
                return data
            except:
                pass
        return {}


# --------------------------------------------------
# FUNCIÓN PRINCIPAL (MODIFICADA)
# --------------------------------------------------

def processTechnicalAnnex():
    input_dir, output_dir = inicioModulo("processGenerateTechnicalAnnex")

    # 1. Cargar inputs
    aligned_file = output_dir / "aligned_concepts.json"
    papers_text_file = output_dir / "papers_text.json"
    papers_metadata_file = output_dir / "papers_metadata.json"
    findings_file = output_dir / "concept_findings.json"   # 🔥 NUEVO

    if not aligned_file.exists():
        writeLog("error", logger, f"[TechAnnex] {aligned_file} not found. Run conceptAlignment first.")
        return
    if not papers_text_file.exists():
        writeLog("error", logger, f"[TechAnnex] {papers_text_file} not found.")
        return

    aligned_data = load_json(aligned_file)
    papers_text = load_json(papers_text_file)
    papers_metadata = load_json(papers_metadata_file) if papers_metadata_file.exists() else []

    # 🔥 NUEVO: Cargar concept_findings.json para obtener all_papers por concepto
    findings_data = None
    if findings_file.exists():
        try:
            findings_data = load_json(findings_file)
            writeLog("info", logger, "[TechAnnex] Loaded concept_findings.json for paper filtering.")
        except Exception as e:
            writeLog("warning", logger, f"[TechAnnex] Could not load concept_findings.json: {e}")

    # Crear un diccionario de concept_id -> lista de paper_ids que tienen evidencias
    # (es decir, all_papers de cada concepto)
    concept_papers_with_evidence = {}
    if findings_data:
        for concept in findings_data:
            concept_id = concept.get("concept_id")
            all_papers = concept.get("all_papers", [])
            if concept_id is not None:
                concept_papers_with_evidence[concept_id] = all_papers
        writeLog("info", logger, f"[TechAnnex] Found evidence papers for {len(concept_papers_with_evidence)} concepts.")

    # 2. Cargar configuración de extracción desde studyDescription.json
    project_config = load_study_config(input_dir)
    fields = project_config.get("technical_extraction", {}).get("fields", [])

    if not fields:
        writeLog("warning", logger, "[TechAnnex] No technical_extraction fields found. Using default hardcoded fields.")
        return False
    else:
        writeLog("info", logger,
                 f"[TechAnnex] Loaded {len(fields)} technical extraction fields from studyDescription.json")

    # Crear diccionarios de lookup
    papers_text_lookup = {p["paper_id"]: p for p in papers_text if "paper_id" in p}
    papers_meta_lookup = {p["paper_id"]: p.get("title", "Unknown Title") for p in papers_metadata if "paper_id" in p}

    # 3. Extraer paper_ids que realmente aparecerán en el anexo
    # Para cada concepto alineado, si hay filter (all_papers), usar solo esos.
    # Si no hay filtro, usar todos los documentos del concepto (comportamiento anterior).
    paper_ids_to_process = set()
    concept_docs_to_include = {}  # concept_id -> lista de doc_ids (ordenados según all_papers)
    for concept in aligned_data.get("aligned_concepts", []):
        concept_id = concept["concept_id"]
        all_docs = concept.get("documents", [])
        all_doc_ids = [doc.get("doc_id") for doc in all_docs if doc.get("doc_id")]

        # Si hay filtro por evidencias, usarlo
        if concept_id in concept_papers_with_evidence:
            filtered_ids = concept_papers_with_evidence[concept_id]
            # Mantener el orden de all_papers (que ya viene ordenado por similitud)
            # Pero solo incluir los que realmente existen en all_docs
            ordered_ids = [pid for pid in filtered_ids if pid in all_doc_ids]
            # Si algunos IDs de all_papers no están en all_docs, se añaden al final (por si acaso)
            for pid in all_doc_ids:
                if pid not in ordered_ids:
                    ordered_ids.append(pid)
            concept_docs_to_include[concept_id] = ordered_ids
            paper_ids_to_process.update(ordered_ids)
            writeLog("info", logger,
                     f"[TechAnnex] Concept {concept_id}: using {len(ordered_ids)} papers with evidence "
                     f"(out of {len(all_doc_ids)} aligned).")
        else:
            # Sin filtro: usar todos los documentos alineados
            concept_docs_to_include[concept_id] = all_doc_ids
            paper_ids_to_process.update(all_doc_ids)
            writeLog("info", logger,
                     f"[TechAnnex] Concept {concept_id}: using all {len(all_doc_ids)} aligned papers (no filter).")

    writeLog("info", logger, f"[TechAnnex] Unique papers to process: {len(paper_ids_to_process)}")

    # 4. Procesar cada paper (con caché en memoria)
    technical_profiles = {}
    processed_count = 0
    skipped_count = 0

    for paper_id in paper_ids_to_process:
        paper = papers_text_lookup.get(paper_id)
        if not paper:
            writeLog("warning", logger, f"[TechAnnex] Paper {paper_id} not found in papers_text.json")
            skipped_count += 1
            continue

        title = papers_meta_lookup.get(paper_id, f"Untitled ({paper_id})")
        writeLog("info", logger,
                 f"[TechAnnex] [{processed_count + 1}/{len(paper_ids_to_process)}] Processing: {title[:60]}...")

        profile = extract_technical_profile(paper, title, fields)
        if profile:
            technical_profiles[paper_id] = {
                "paper_id": paper_id,
                "title": title,
                "profile": profile
            }
        else:
            # Guardar vacío para evitar reprocesar si se vuelve a ejecutar
            technical_profiles[paper_id] = {
                "paper_id": paper_id,
                "title": title,
                "profile": {}
            }
        processed_count += 1

        # Pequeña pausa para no saturar el LLM
        time.sleep(SLEEP_BETWEEN_CALLS)

    writeLog("info", logger, f"[TechAnnex] Processed {processed_count} papers. Skipped {skipped_count}.")

    # 5. Mapear perfiles a los conceptos para facilitar la salida en trainingMaterials
    concepts_with_tech = []
    for concept in aligned_data.get("aligned_concepts", []):
        concept_id = concept["concept_id"]
        concept_name = concept.get("concept_name", "")
        concept_query = concept.get("concept_query", "")

        # Obtener la lista ordenada de doc_ids para este concepto
        doc_ids = concept_docs_to_include.get(concept_id, [])
        concept_papers = []
        for doc_id in doc_ids:
            if doc_id in technical_profiles:
                concept_papers.append(technical_profiles[doc_id])
            else:
                # Si no se procesó (por ejemplo, no encontrado en papers_text), se añade vacío
                concept_papers.append({
                    "paper_id": doc_id,
                    "title": papers_meta_lookup.get(doc_id, "Unknown"),
                    "profile": {}
                })

        concepts_with_tech.append({
            "concept_id": concept_id,
            "concept_name": concept_name,
            "concept_query": concept_query,
            "papers": concept_papers
        })

    # 6. Guardar resultado
    output_data = {
        "schema_version": "1.1",  # 🔥 versión actualizada para indicar el filtro
        "filtered_by_evidence": bool(findings_data),  # 🔥 indicador de que se aplicó filtro
        "n_concepts": len(concepts_with_tech),
        "concepts": concepts_with_tech
    }

    output_file = output_dir / "technical_annex.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    writeLog("info", logger, f"[TechAnnex] Saved to {output_file}")
    return output_data


if __name__ == "__main__":
    processTechnicalAnnex()