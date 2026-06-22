# trainingMaterials.py
from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo

import json
from pathlib import Path
from datetime import datetime
import re
import requests
import time
from functools import lru_cache

# --------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------
OFF_TOPIC_THRESHOLD = 0.30
MAX_CHARS_PER_EMERGING = 8000
TRANSLATE_EVIDENCES = False  # Las evidencias ya vienen traducidas de synthesize_findings
MAX_EVIDENCES_TO_SHOW = 30   # Número máximo de evidencias a mostrar por bloque

# 🔥 NUEVO: Umbral mínimo de similitud para mostrar una evidencia en el informe
# Las evidencias con similitud inferior a este valor NO se mostrarán.
MIN_DISPLAY_SIMILARITY = 2.5   # Ajusta según necesidad (valores típicos entre 1.0 y 3.0)

# Para traducción (solo si se necesita para emergentes o por si acaso)
LLM_CONFIG = {
    "backend": "ollama",
    "model": "llama3.2:3b",
    "url": "http://localhost:11434/api/generate",
}

# --------------------------------------------------
# FUNCIONES AUXILIARES
# --------------------------------------------------
def load_json(file_path: Path) -> dict:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)

def clean_text_for_markdown(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text).strip()

def call_llm(prompt: str, max_retries: int = 2) -> str:
    for attempt in range(max_retries):
        try:
            response = requests.post(
                LLM_CONFIG["url"],
                json={
                    "model": LLM_CONFIG["model"],
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.2, "num_predict": 1024},
                },
                timeout=90,
            )
            if response.status_code == 200:
                result = response.json().get("response", "").strip()
                if result:
                    return result
            else:
                writeLog("warning", logger, f"LLM error {response.status_code} (attempt {attempt + 1})")
        except Exception as e:
            writeLog("warning", logger, f"LLM exception (attempt {attempt + 1}): {e}")
        time.sleep(2)
    return ""

@lru_cache(maxsize=100)
def translate_to_spanish(text: str) -> str:
    if not text or len(text.strip()) < 10:
        return text
    clean_text = re.sub(r'\s+', ' ', text).strip()
    if len(clean_text) > 3000:
        clean_text = clean_text[:3000] + "..."
    prompt = f"""Traduce el siguiente texto académico del inglés al español. Mantén el tono formal, la terminología técnica y la puntuación. Devuelve solo la traducción.

Texto original:
{clean_text}

Traducción al español:"""
    translated = call_llm(prompt)
    return translated if translated else text

def format_evidence_strength(avg_score: float, evidence_count: int) -> tuple[str, str]:
    if evidence_count >= 5 and avg_score >= 0.70:
        return ("Muy Alta", "🔴")
    elif evidence_count >= 3 and avg_score >= 0.65:
        return ("Alta", "🟠")
    elif evidence_count >= 2 and avg_score >= 0.60:
        return ("Media", "🟡")
    elif evidence_count >= 1:
        return ("Baja", "🟢")
    else:
        return ("Sin evidencia", "⚪")

# --------------------------------------------------
# FORMATEO DE EVIDENCIAS (reutilizable)
# --------------------------------------------------
def format_evidence_blocks(evidences: list, limit: int = MAX_EVIDENCES_TO_SHOW) -> str:
    """Genera bloques de markdown para una lista de evidencias (con estructura incluida).
       Filtra las evidencias por MIN_DISPLAY_SIMILARITY.
    """
    if not evidences:
        return "No hay evidencias disponibles."

    # Filtrar por similitud mínima
    filtered = [ev for ev in evidences if ev.get("similarity", 0) >= MIN_DISPLAY_SIMILARITY]
    if not filtered:
        return f"No hay evidencias con similitud ≥ {MIN_DISPLAY_SIMILARITY}."

    # Limitar
    evidences_to_show = filtered[:limit]

    blocks = []
    for ev in evidences_to_show:
        evidence_text = clean_text_for_markdown(ev['text'])
        structured = ev.get("structured_evidence", {})
        estructurado_md = ""
        if structured:
            ideas = structured.get("ideas", "")
            metodos = structured.get("metodos", "")
            resultados = structured.get("resultados", "")
            aplicaciones = structured.get("aplicaciones", "")
            estructurado_md = f"""
**Ideas principales:** {ideas} **Métodos:** {metodos} **Resultados:** {resultados} **Aplicaciones relevantes:** {aplicaciones}
            """
        blocks.append(f"""
- **{ev['paper_title']}** (similitud: {ev['similarity']:.3f}) (ID: `{ev['doc_id']}`)
*“{evidence_text}”*  

{estructurado_md}
        """)
    return "\n".join(blocks)

# --------------------------------------------------
# EVALUACIÓN DE LA QUERY
# --------------------------------------------------
def evaluate_search_strategy(concepts_query: dict, aligned_concepts: dict, candidate_concepts: dict) -> tuple[
    str, list]:
    query_obj = concepts_query.get("query", "No disponible")
    query_obj = clean_text_for_markdown(query_obj)
    n_objective_concepts = len(concepts_query.get("concepts", []))
    n_candidates = candidate_concepts.get("n_topics", 0)
    n_aligned = len(aligned_concepts.get("aligned_concepts", []))
    threshold = aligned_concepts.get("alignment_threshold", OFF_TOPIC_THRESHOLD)
    min_docs = aligned_concepts.get("min_docs_after_rerank", "No especificado")
    focus_terms_used = aligned_concepts.get("focus_terms_used", False)

    aligned_ids = set()
    for ac in aligned_concepts.get("aligned_concepts", []):
        aligned_ids.add(ac.get("concept_id"))
    candidate_list = candidate_concepts.get("concepts", [])
    emergent_ids = [c.get("concept_id") for c in candidate_list if c.get("concept_id") not in aligned_ids]
    n_emergent = len(emergent_ids)

    evaluation = f"""
## Evaluación de la estrategia de búsqueda

### Query utilizada
{query_obj}    

### Estadísticas de recuperación y filtrado
- **Conceptos objetivo definidos:** {n_objective_concepts}
- **Tópicos descubiertos (`candidate_concepts`):** {n_candidates}
- **Tópicos alineados con conceptos objetivo:** {n_aligned}
- **Tópicos emergentes no alineados:** {n_emergent}
- **Umbral de relevancia aplicado:** {threshold}
- **Mínimo de documentos retenidos por concepto (re‑ranking):** {min_docs}
- **Focus terms utilizados:** {"Sí" if focus_terms_used else "No"}

    """
    return evaluation, emergent_ids

# --------------------------------------------------
# GENERACIÓN DE TARJETAS DE HALLAZGOS (MÚLTIPLES HALLAZGOS)
# --------------------------------------------------
def generate_training_card(concept_data: dict) -> str:
    concept_id = concept_data["concept_id"]
    concept_query = concept_data["concept_query"]
    findings = concept_data.get("findings", [])
    n_findings = concept_data.get("n_findings", 0)
    all_evidences = concept_data.get("all_evidences", [])
    all_papers = concept_data.get("all_papers", [])

    # 🔥 Filtrar all_papers: solo aquellos que tienen al menos una evidencia que supere MIN_DISPLAY_SIMILARITY
    # (para construir la cabecera de papers representados)
    # Obtenemos los doc_id de todas las evidencias que pasan el filtro
    valid_evidences = [ev for ev in all_evidences if ev.get("similarity", 0) >= MIN_DISPLAY_SIMILARITY]
    valid_paper_ids = set(ev["doc_id"] for ev in valid_evidences)
    # Si no hay hallazgos y all_evidences vacío, no hay papers válidos
    if not valid_paper_ids and n_findings > 0:
        # Si hay hallazgos, intentar obtener los papers de sus top_quotes (ya filtradas)
        valid_paper_ids = set()
        for finding in findings:
            for quote in finding.get("top_quotes", []):
                if quote.get("similarity", 0) >= MIN_DISPLAY_SIMILARITY:
                    valid_paper_ids.add(quote["doc_id"])
    # Si aún así no hay, usar all_papers (pero puede contener papers de baja similitud)
    # En ese caso, se mostrarán pero con advertencia; es mejor no mostrar ninguno si no pasan el filtro.
    display_papers = list(valid_paper_ids) if valid_paper_ids else []

    # Cabecera común
    header = f"""
## Concepto {concept_id}: {concept_query}
    """
    if display_papers:
        paper_list = ', '.join(display_papers[:15])
        if len(display_papers) > 15:
            paper_list += f" ... y {len(display_papers) - 15} más"
        header += f"""
**Papers representados en las evidencias:** {len(display_papers)} documentos: `{paper_list}`

"""

    # Caso 1: Sin hallazgos, pero con evidencias sueltas (all_evidences)
    if n_findings == 0:
        # Filtrar all_evidences para mostrar solo las que pasan el umbral
        filtered_all = [ev for ev in all_evidences if ev.get("similarity", 0) >= MIN_DISPLAY_SIMILARITY]
        if filtered_all:
            return f"""{header}
### Estado: SIN HALLAZGOS SINTETIZADOS (evidencias sueltas disponibles)

No se generaron hallazgos consolidados para este concepto, pero se encontraron fragmentos relevantes que pueden ser de interés.

**Evidencias textuales relevantes:**

{format_evidence_blocks(filtered_all)}
            """
        else:
            return f"""{header}
### Estado: SIN EVIDENCIA SUFICIENTE

No se encontraron hallazgos ni evidencias con similitud suficiente para este concepto en el corpus actual.
            """

    # Caso 2: Múltiples hallazgos
    card_parts = []
    for idx, finding_data in enumerate(findings, 1):
        finding_text_es = finding_data["finding"]
        evidence_count = finding_data["evidence_count"]
        avg_score = finding_data["avg_evidence_score"]
        strength, emoji = format_evidence_strength(avg_score, evidence_count)
        top_quotes = finding_data.get("top_quotes", [])

        # Filtrar top_quotes por similitud mínima
        filtered_top = [q for q in top_quotes if q.get("similarity", 0) >= MIN_DISPLAY_SIMILARITY]
        if not filtered_top:
            # Si no hay citas que pasen el filtro, omitimos este hallazgo (o mostramos un mensaje)
            # Pero es mejor omitirlo directamente
            continue

        card_parts.append(f"""
### Hallazgo {idx}: {strength}

> {finding_text_es}

**Evidencias textuales que respaldan este hallazgo:**

{format_evidence_blocks(filtered_top)}
        """)

    if not card_parts:
        return f"""{header}
### Estado: SIN EVIDENCIA SUFICIENTE

Los hallazgos generados no tienen citas con similitud suficiente para ser mostradas.
        """

    return header + "\n---\n".join(card_parts)

# --------------------------------------------------
# GENERACIÓN DE TÍTULO PARA EMERGENTES
# --------------------------------------------------
def generate_emerging_title(concept: dict, papers_text: dict) -> str:
    """
    Genera un título descriptivo (máximo 10 palabras) para un concepto emergente.
    """
    paper_ids = concept.get("document_indices", [])
    if not paper_ids:
        return concept.get("label", "Concepto emergente")

    # Extraer fragmentos de los papers para dar contexto al LLM
    texts = []
    for pid in paper_ids:
        paper = papers_text.get(pid)
        if not paper:
            continue
        parts = [
            paper.get("title", ""),
            paper.get("abstract", ""),
        ]
        full = " ".join(parts)
        if len(full) > 1000:
            full = full[:1000]
        texts.append(full)

    combined = "\n\n".join(texts)
    if len(combined) > 3000:
        combined = combined[:3000]
    if not combined.strip():
        return concept.get("label", "Concepto emergente")

    prompt = f"""Eres un asistente de investigación. A continuación tienes fragmentos de artículos científicos que pertenecen a un tópico emergente.

Genera un TÍTULO CONCISO Y DESCRIPTIVO (máximo 10 palabras) que capture la esencia del tema. Debe ser en español y sin asteriscos ni negritas.

Fragmentos:
{combined}

Título:"""
    response = call_llm(prompt)
    if not response:
        return concept.get("label", "Concepto emergente")
    # Limpiar posibles asteriscos o negritas
    title = re.sub(r'[*#]', '', response).strip()
    # Si el título es demasiado largo, truncar
    if len(title) > 100:
        title = title[:100]
    return title if title else concept.get("label", "Concepto emergente")

# --------------------------------------------------
# GENERACIÓN DE DESCRIPCIÓN PARA EMERGENTES (versión original detallada)
# --------------------------------------------------
def generate_emerging_description(concept: dict, papers_text: dict) -> str:
    """Genera descripción en español usando el LLM (versión original detallada)."""
    paper_ids = concept.get("document_indices", [])
    if not paper_ids:
        return "No hay papers asociados a este concepto."
    texts = []
    for pid in paper_ids:
        paper = papers_text.get(pid)
        if not paper:
            continue
        parts = [
            paper.get("title", ""),
            paper.get("abstract", ""),
            paper.get("clean_sections", {}).get("introduction", ""),
            paper.get("clean_sections", {}).get("conclusion", ""),
        ]
        full = " ".join(parts)
        if len(full) > 1500:
            full = full[:1500]
        texts.append(full)
    combined = "\n\n".join(texts)
    if len(combined) > MAX_CHARS_PER_EMERGING:
        combined = combined[:MAX_CHARS_PER_EMERGING]
    if not combined.strip():
        return "No hay texto suficiente para analizar."

    prompt = f"""Eres un asistente de investigación. Resume en ESPAÑOL, de forma clara y concisa, el siguiente conjunto de fragmentos de artículos científicos que pertenecen al tema **"{concept.get('label', 'Concepto emergente')}"**.

Extrae las ideas principales, métodos, resultados y aplicaciones relevantes. Genera un texto continuo y comprensible de unas 200-300 palabras.

Fragmentos:
{combined}
"""
    response = call_llm(prompt)
    if not response:
        return "No se pudo generar una descripción automática."
    return clean_text_for_markdown(response)

# --------------------------------------------------
# CONCEPTOS EMERGENTES (actualizado)
# --------------------------------------------------
def generate_emerging_section(emergent_ids: list, candidate_concepts: dict, papers_text: dict) -> str:
    if not emergent_ids:
        return "## Conceptos emergentes no alineados\n\nNo se detectaron conceptos emergentes fuera de los objetivos.\n"

    candidate_list = candidate_concepts.get("concepts", [])
    emergent_concepts = [c for c in candidate_list if c.get("concept_id") in emergent_ids]
    if not emergent_concepts:
        return "## Conceptos emergentes no alineados\n\nNo se encontraron metadatos de los conceptos emergentes.\n"

    section = "## Conceptos emergentes (no alineados con sus conceptos objetivo)\n\n"
    section += "Los siguientes tópicos fueron descubiertos automáticamente en los documentos, pero no superaron el umbral de alineamiento. Pueden indicar áreas relacionadas que merecen atención o servir para afinar sus consultas.\n\n"

    for i, concept in enumerate(emergent_concepts, 1):
        # 🔥 Generar título con la nueva función específica
        title = generate_emerging_title(concept, papers_text)
        # 🔥 Usar la función original para la descripción detallada
        description = generate_emerging_description(concept, papers_text)

        keywords = ", ".join(concept.get("keywords", [])[:8])
        paper_ids = concept.get("document_indices", [])
        n_papers = len(paper_ids)
        ids_str = ", ".join(paper_ids[:10])
        if len(paper_ids) > 10:
            ids_str += " ..."

        section += f"""
### Emergente {i}: {title}
- **Palabras clave:** {keywords}
- **Papers asociados ({n_papers}):** `{ids_str}`
- **Descripción sintética:**  
      {description}

    """
    return section

# --------------------------------------------------
# ANEXO TÉCNICO (usa technical_annex.json)
# --------------------------------------------------
def generate_technical_annex_markdown(concepts_with_tech: list) -> str:
    if not concepts_with_tech:
        return "## Anexo Técnico: Metodologías y Experimentación\n\nNo se encontraron datos técnicos para los conceptos alineados.\n"

    sections = []
    sections.append("\n---\n\n## Anexo Técnico: Metodologías y Experimentación\n")
    sections.append(
        "A continuación se detallan los aspectos metodológicos y de experimentación extraídos de los papers asociados a cada concepto alineado.\n\n")

    for concept in concepts_with_tech:
        concept_id = concept.get("concept_id")
        concept_name = concept.get("concept_name", "")
        papers = concept.get("papers", [])

        if not papers:
            continue

        sections.append(f"\n---\n### Concepto {concept_id}: {concept_name}\n\n")
        sections.append(
            "| ID | Dispositivos Sensores | Modalidades | Extracción de Características | Modelos ML/DL | Rendimiento | Limitaciones |")
        sections.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")

        for paper in papers[:10]:
            paper_id = paper.get("paper_id", "Unknown")
            profile = paper.get("profile", {})

            def join_field(field):
                if isinstance(field, list):
                    return ", ".join(field) or "—"
                elif isinstance(field, str):
                    return field or "—"
                else:
                    return "—"

            devices = join_field(profile.get("sensor_devices"))
            modalities = join_field(profile.get("sensor_modalities"))
            features = join_field(profile.get("feature_extraction_methods"))
            models = join_field(profile.get("machine_learning_models"))

            perf_raw = profile.get("performance_metrics")
            if perf_raw is None:
                performance = "—"
            elif isinstance(perf_raw, list):
                performance = ", ".join(perf_raw) or "—"
            else:
                performance = str(perf_raw) or "—"

            lim_raw = profile.get("limitations")
            if lim_raw is None:
                limitations = "—"
            elif isinstance(lim_raw, list):
                limitations = ", ".join(lim_raw) or "—"
            else:
                limitations = str(lim_raw) or "—"

            devices = devices.replace("|", "\\|")
            modalities = modalities.replace("|", "\\|")
            features = features.replace("|", "\\|")
            models = models.replace("|", "\\|")
            performance = performance.replace("|", "\\|")
            limitations = limitations.replace("|", "\\|")

            sections.append(
                f"| `{paper_id}` | {devices} | {modalities} | {features} | {models} | {performance} | {limitations} |")

        sections.append("\n")

    return "\n".join(sections)

# --------------------------------------------------
# DOCUMENTO FINAL
# --------------------------------------------------
def generate_training_document(
        findings_data: list,
        concepts_query: dict,
        aligned_concepts: dict,
        candidate_concepts: dict,
        concepts_with_tech: list,
        papers_text: dict
) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    search_eval, emergent_ids = evaluate_search_strategy(concepts_query, aligned_concepts, candidate_concepts)

    doc = f"""# Scientific Literature Intelligence Pipeline (SLIP)

**Generado:** {timestamp}\n

---

{search_eval}

---

## Resumen de hallazgos por concepto objetivo
"""
    # 🔥 NUEVO: Lista en texto plano (no tabla)
    for concept in findings_data:
        concept_id = concept["concept_id"]
        q_short = concept["concept_query"]
        n_f = concept.get("n_findings", 0)
        status = "✅ Con hallazgos" if n_f > 0 else "Sin hallazgos"
        # Obtener número de papers únicos (all_papers)
        papers_count = len(concept.get("all_papers", []))
        # Obtener texto del primer hallazgo (si existe)
        findings_list = concept.get("findings", [])
        if findings_list:
            first_finding = findings_list[0].get("finding", "")[:150] + "..." if len(findings_list[0].get("finding", "")) > 150 else findings_list[0].get("finding", "")
        else:
            first_finding = "Sin hallazgos sintetizados"
        doc += f"""
**Concepto {concept_id}:** {q_short}\n
- Hallazgos: {n_f} | Papers representados: {papers_count}
- Resumen: {first_finding}
"""

    doc += "\n---\n"

    for concept in findings_data:
        doc += generate_training_card(concept)
        doc += "\n---\n"

    doc += generate_emerging_section(emergent_ids, candidate_concepts, papers_text)
    doc += "\n---\n"

    doc += generate_technical_annex_markdown(concepts_with_tech)
    doc += "\n---\n"

    # Bibliografía (de los papers representados en las evidencias que pasan el filtro)
    doc += "\n## Bibliografía completa de los hallazgos principales\n"
    all_papers = {}
    for concept in findings_data:
        # Obtener todos los doc_id de evidencias que superan el filtro
        for ev in concept.get("all_evidences", []):
            if ev.get("similarity", 0) >= MIN_DISPLAY_SIMILARITY:
                all_papers[ev["doc_id"]] = ev["paper_title"]
        # También añadir los de top_quotes de los findings (que ya pasan el filtro)
        for finding in concept.get("findings", []):
            for quote in finding.get("top_quotes", []):
                if quote.get("similarity", 0) >= MIN_DISPLAY_SIMILARITY:
                    all_papers[quote["doc_id"]] = quote["paper_title"]
    if all_papers:
        for doc_id, title in sorted(all_papers.items(), key=lambda x: x[1].lower()):
            doc += f"- **{title}**  \n  (ID: `{doc_id}`)\n\n"
    else:
        doc += "No se encontraron evidencias con similitud suficiente.\n"

    doc += "\n*Documento generado automáticamente por SLIP. Los títulos de los papers se mantienen en su idioma original; los fragmentos de evidencia y los resúmenes están traducidos al español.*\n"
    return doc

# --------------------------------------------------
# ENTRY POINT
# --------------------------------------------------
def processTrainingMaterials():
    input_dir, output_dir = inicioModulo("processTrainingMaterials")

    findings_file = output_dir / "concept_findings.json"
    candidate_file = output_dir / "candidate_concepts.json"
    aligned_file = output_dir / "aligned_concepts.json"
    query_file = input_dir / "conceptsQuery.json"
    technical_file = output_dir / "technical_annex.json"
    papers_file = output_dir / "papers_text.json"

    # Cargar hallazgos (obligatorio)
    if not findings_file.exists():
        writeLog("error", logger, f"[Training] No se encuentra {findings_file}")
        return
    findings_data = load_json(findings_file)

    # Cargar otros archivos (opcionales)
    candidate_concepts = load_json(candidate_file) if candidate_file.exists() else {"concepts": [], "n_topics": 0}
    aligned_concepts = load_json(aligned_file) if aligned_file.exists() else {"aligned_concepts": [],
                                                                              "alignment_threshold": OFF_TOPIC_THRESHOLD}
    concepts_query = load_json(query_file) if query_file.exists() else {"query": "", "concepts": []}
    concepts_with_tech = load_json(technical_file).get("concepts", []) if technical_file.exists() else []

    # Cargar papers_text (necesario para emergentes)
    papers_text = {}
    if candidate_concepts.get("concepts") and papers_file.exists():
        papers_data = load_json(papers_file)
        papers_text = {p["paper_id"]: p for p in papers_data if "paper_id" in p}

    doc = generate_training_document(
        findings_data,
        concepts_query,
        aligned_concepts,
        candidate_concepts,
        concepts_with_tech,
        papers_text
    )

    output_file = output_dir / "training_materials.md"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(doc)
    writeLog("info", logger, f"[Training] Material guardado en {output_file}")
    print(f"\n📄 Documento completo (en español): {output_file}")


if __name__ == "__main__":
    processTrainingMaterials()