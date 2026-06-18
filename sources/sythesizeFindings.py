# synthesize_findings.py
from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo

import json
import ollama
import numpy as np
from pathlib import Path
from sentence_transformers import SentenceTransformer
import requests
import time
import re
from functools import lru_cache

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
LLM_MODEL = "llama3.1:8b"
RELEVANCE_PERCENTILE = 0.6               # Conserva el 60% de los hallazgos generados
FOCUS_TERM_BOOST_WEIGHT = 0.15
MAX_STRUCTURED_TEXT = 1500
MAX_EVIDENCES_PER_CLUSTER = 12            # Fragmentos por cluster para generar hallazgos
MAX_EVIDENCES_PER_CONCEPT = 30            # Límite total de evidencias en el informe (se aplica en trainingMaterials)

# Para traducción
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

@lru_cache(maxsize=200)
def translate_to_spanish(text: str) -> str:
    if not text or len(text.strip()) < 10:
        return text
    clean_text = re.sub(r'\s+', ' ', text).strip()
    if len(clean_text) > 3000:
        clean_text = clean_text[:3000] + "..."
    prompt = f"""Traduce el siguiente texto académico del inglés al español. Mantén el tono formal, la terminología técnica y la puntuación. Devuelve solo la traducción.

Texto original:
{clean_text}

Traducción:"""
    translated = call_llm(prompt)
    return translated if translated else text

def load_focus_terms(input_dir: Path) -> list[str]:
    json_file = input_dir / "studyDescription.json"
    if json_file.exists():
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("project", {}).get("focus_terms", [])
        except Exception as e:
            writeLog("error", logger, f"[Synthesis] Error loading focus_terms: {e}")
    return []

def load_clustered_evidences(input_file: Path) -> list:
    with open(input_file, 'r', encoding='utf-8') as f:
        return json.load(f)

def load_papers_text(output_dir: Path) -> dict:
    papers_file = output_dir / "papers_text.json"
    if not papers_file.exists():
        writeLog("error", logger, f"[Synthesis] papers_text.json not found")
        return {}
    data = load_json(papers_file)
    return {p["paper_id"]: p for p in data if "paper_id" in p}

# --------------------------------------------------
# EXTRACCIÓN DE ESTRUCTURA DEL PAPER
# --------------------------------------------------

def extract_structured_evidence(paper_id: str, papers_text: dict, concept_query: str) -> dict:
    paper = papers_text.get(paper_id)
    if not paper:
        return {}
    sections = paper.get("clean_sections", {})
    result = {}
    mapping = {
        "ideas": ["introduction", "background", "abstract"],
        "metodos": ["methods", "methodology", "experimental", "procedure"],
        "resultados": ["results", "findings", "evaluation"],
        "aplicaciones": ["discussion", "conclusion", "application"]
    }
    for key, candidates in mapping.items():
        for candidate in candidates:
            if candidate in sections:
                result[key] = sections[candidate]
                break
        if key not in result:
            if key == "ideas":
                result[key] = paper.get("abstract", "") or paper.get("clean_text", "")[:2000]
            else:
                result[key] = ""
    # Traducir al español
    for k in result:
        if result[k]:
            result[k] = translate_to_spanish(result[k][:MAX_STRUCTURED_TEXT])
    return result

# --------------------------------------------------
# GENERACIÓN DE HALLAZGO POR CLUSTER (solo para clusters >= 2)
# --------------------------------------------------

def generate_finding_from_cluster(
    concept_id: int,
    concept_query: str,
    cluster: dict,
    model: str = LLM_MODEL,
    focus_terms: list[str] = None,
    papers_text: dict = None
) -> dict:
    evidence_texts = [ev["text"] for ev in cluster["evidences"]]
    doc_to_title = dict(zip(cluster.get("papers", []), cluster.get("paper_titles", [])))

    # Seleccionar las mejores MAX_EVIDENCES_PER_CLUSTER evidencias
    top_evidences = sorted(cluster["evidences"], key=lambda x: x["similarity"], reverse=True)[:MAX_EVIDENCES_PER_CLUSTER]

    top_quotes = []
    for ev in top_evidences:
        structured = {}
        if papers_text:
            structured = extract_structured_evidence(ev["doc_id"], papers_text, concept_query)
        top_quotes.append({
            "text": ev["text"],
            "doc_id": ev["doc_id"],
            "paper_title": doc_to_title.get(ev["doc_id"], "Unknown"),
            "similarity": ev["similarity"],
            "structured_evidence": structured
        })

    # Generar el hallazgo resumen (texto compendio)
    focus_instruction = ""
    if focus_terms:
        focus_terms_str = ", ".join(focus_terms[:10])
        focus_instruction = f"""
Special instructions for this research:
- The core focus of this thesis is on sensor-based data (especially: {focus_terms_str}).
- When synthesizing the finding, prioritize aspects related to these sensor terms if they appear in the evidence.
- If the evidence does not mention any of these terms, simply summarize the evidence without forcing them.
"""
    prompt = f"""You are an expert researcher synthesizing academic literature.

Answer the following research question based ONLY on the evidence snippets provided.
Do not add any external knowledge, speculation, or information not present in the evidence.
{focus_instruction}

Research question: {concept_query}

Number of supporting evidences: {cluster["size"]}
Average evidence quality score: {cluster["avg_evidence_score"]}

Evidence snippets:
{chr(10).join([f'- {text[:500]}' for text in evidence_texts])}

Instructions:
Write a concise summary (2-4 sentences) that captures the main contribution, key findings, and practical implications.
Output only the summary in Spanish (but keep technical terms in English if needed).
Summary:"""

    try:
        response = ollama.generate(
            model=model,
            prompt=prompt,
            options={
                "temperature": 0.3,
                "num_predict": 300,
            }
        )
        finding_text = response['response'].strip()
        # Limpiar prefijos
        prefixes = ["Summary:", "summary:", "Resumen:", "resumen:"]
        for p in prefixes:
            if finding_text.startswith(p):
                finding_text = finding_text[len(p):].strip()
        if not finding_text:
            finding_text = f"Evidence cluster ({cluster['size']} items) suggests: {evidence_texts[0][:200]}..."
    except Exception as e:
        writeLog("warning", logger, f"[Synthesis] Error generating finding: {e}")
        finding_text = f"Unable to generate finding: {evidence_texts[0][:200]}..."

    return {
        "finding": finding_text,
        "supporting_papers": list(set(ev["doc_id"] for ev in cluster["evidences"])),
        "evidence_count": cluster["size"],
        "avg_evidence_score": cluster["avg_evidence_score"],
        "top_quotes": top_quotes   # hasta MAX_EVIDENCES_PER_CLUSTER
    }

# --------------------------------------------------
# FILTRADO POR RELEVANCIA
# --------------------------------------------------

def is_finding_relevant_to_concept(
    finding_text: str,
    concept_query: str,
    embedding_model: SentenceTransformer,
    focus_terms: list[str] = None
) -> float:
    finding_embedding = embedding_model.encode(finding_text, normalize_embeddings=True)
    concept_embedding = embedding_model.encode(concept_query, normalize_embeddings=True)
    sim = float(np.dot(finding_embedding, concept_embedding))
    if focus_terms:
        lower_finding = finding_text.lower()
        focus_hits = sum(1 for term in focus_terms if term.lower() in lower_finding)
        sim += focus_hits * FOCUS_TERM_BOOST_WEIGHT
    return sim

def filter_findings_by_relevance(
    findings: list,
    concept_query: str,
    embedding_model: SentenceTransformer,
    focus_terms: list[str] = None,
    percentile_threshold: float = RELEVANCE_PERCENTILE
) -> list:
    if not findings:
        return []
    similarities = [is_finding_relevant_to_concept(f["finding"], concept_query, embedding_model, focus_terms) for f in findings]
    dynamic_threshold = np.percentile(similarities, (1 - percentile_threshold) * 100)
    writeLog("info", logger,
             f"[Filter] {len(findings)} findings, threshold={dynamic_threshold:.3f} "
             f"(percentile {percentile_threshold * 100:.0f}% kept)")
    return [f for f, sim in zip(findings, similarities) if sim >= dynamic_threshold]

# --------------------------------------------------
# SÍNTESIS POR CONCEPTO (incluye clusters de tamaño 1 en all_evidences)
# --------------------------------------------------

def synthesize_concept_no_filter(
    concept_data: dict,
    llm_model: str,
    focus_terms: list[str] = None,
    papers_text: dict = None
) -> dict:
    concept_id = concept_data["concept_id"]
    concept_query = concept_data["concept_query"]
    clusters = concept_data.get("clusters", [])

    # Separar clusters por tamaño
    clusters_for_findings = [c for c in clusters if c["size"] >= 2]   # Para generar hallazgos
    all_clusters = clusters  # Todos los clusters, incluyendo tamaño 1, para recoger evidencias

    # Diccionario para evitar duplicados en all_evidences (clave: (doc_id, text))
    # Aseguramos que ambos elementos sean hashables: convertimos doc_id a str y text a str
    all_evidences_dict = {}

    # 1. Generar hallazgos solo para clusters >= 2
    raw_findings = []
    for cluster in clusters_for_findings:
        finding = generate_finding_from_cluster(
            concept_id,
            concept_query,
            cluster,
            llm_model,
            focus_terms,
            papers_text
        )
        raw_findings.append(finding)
        # Recolectar top_quotes de este cluster
        for quote in finding["top_quotes"]:
            # Asegurar que la clave sea hashable
            key = (str(quote["doc_id"]), str(quote["text"]))
            if key not in all_evidences_dict or quote["similarity"] > all_evidences_dict[key]["similarity"]:
                all_evidences_dict[key] = quote

    # 2. Recolectar evidencias de clusters de tamaño 1 (sin generar hallazgo)
    for cluster in all_clusters:
        if cluster["size"] == 1:
            ev = cluster["evidences"][0]
            # Extraer estructura si es posible
            structured = {}
            if papers_text:
                structured = extract_structured_evidence(ev["doc_id"], papers_text, concept_query)
            quote = {
                "text": ev["text"],
                "doc_id": ev["doc_id"],
                "paper_title": cluster.get("paper_titles", [""])[0] if cluster.get("paper_titles") else "Unknown",
                "similarity": ev["similarity"],
                "structured_evidence": structured
            }
            key = (str(quote["doc_id"]), str(quote["text"]))
            if key not in all_evidences_dict or quote["similarity"] > all_evidences_dict[key]["similarity"]:
                all_evidences_dict[key] = quote

    # Ordenar todas las evidencias por similitud
    all_evidences = sorted(all_evidences_dict.values(), key=lambda x: x["similarity"], reverse=True)
    all_papers = list(set(ev["doc_id"] for ev in all_evidences))

    return {
        "concept_id": concept_id,
        "concept_query": concept_query,
        "total_evidences": concept_data["total_evidences"],
        "n_findings_raw": len(raw_findings),
        "findings_raw": raw_findings,
        "all_evidences": all_evidences,
        "all_papers": all_papers
    }

# --------------------------------------------------
# ENTRY POINT
# --------------------------------------------------

def processSynthesizeFindings():
    input_dir, output_dir = inicioModulo("processSynthesizeFindings")
    input_file = output_dir / "clustered_evidences.json"
    if not input_file.exists():
        writeLog("error", logger, f"[Synthesis] No se encuentra {input_file}")
        return

    focus_terms = load_focus_terms(input_dir)
    papers_text = load_papers_text(output_dir)

    writeLog("info", logger, f"[Synthesis] PASS 1: Generating findings with {LLM_MODEL}...")
    data = load_clustered_evidences(input_file)

    raw_results = []
    for concept_data in data:
        result = synthesize_concept_no_filter(
            concept_data,
            LLM_MODEL,
            focus_terms,
            papers_text
        )
        raw_results.append(result)

    # PASS 2: Filtrar hallazgos por relevancia
    writeLog("info", logger, "[Synthesis] PASS 2: Loading embedding model for relevance filtering...")
    embedding_model = SentenceTransformer(EMBEDDING_MODEL)

    filtered_results = []
    for result in raw_results:
        if result.get("n_findings_raw", 0) == 0:
            filtered_results.append({
                "concept_id": result["concept_id"],
                "concept_query": result["concept_query"],
                "total_evidences": result["total_evidences"],
                "focus_terms_used": focus_terms or [],
                "n_findings": 0,
                "findings": [],
                "all_evidences": result["all_evidences"],
                "all_papers": result["all_papers"]
            })
            continue

        filtered_findings = filter_findings_by_relevance(
            result["findings_raw"],
            result["concept_query"],
            embedding_model,
            focus_terms,
            RELEVANCE_PERCENTILE
        )
        # Si no quedan hallazgos, conservamos al menos el mejor
        if not filtered_findings and result["findings_raw"]:
            filtered_findings = [result["findings_raw"][0]]
            writeLog("info", logger, f"[Synthesis] Concept {result['concept_id']}: No findings passed filter, keeping best one.")

        filtered_results.append({
            "concept_id": result["concept_id"],
            "concept_query": result["concept_query"],
            "total_evidences": result["total_evidences"],
            "focus_terms_used": focus_terms or [],
            "n_findings": len(filtered_findings),
            "findings": filtered_findings,
            "all_evidences": result["all_evidences"],   # todas las evidencias de todos los clusters
            "all_papers": result["all_papers"]          # IDs únicos de papers representados
        })

        writeLog("info", logger,
                 f"[Synthesis] Concept {result['concept_id']}: "
                 f"{result['n_findings_raw']} generated → {len(filtered_findings)} after filtering, "
                 f"{len(result['all_evidences'])} total evidences collected from {len(result['all_papers'])} papers")

    output_file = output_dir / "concept_findings.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(filtered_results, f, indent=2, ensure_ascii=False)
    writeLog("info", logger, f"[Synthesis] Saved to {output_file}")

    # Summary
    writeLog("info", logger, "\n" + "=" * 60)
    writeLog("info", logger, "RESUMEN DE SÍNTESIS")
    writeLog("info", logger, "=" * 60)
    for result in filtered_results:
        writeLog("info", logger, f"\nConcepto {result['concept_id']}: {result['concept_query']}")
        writeLog("info", logger, f"  Evidencias totales: {result['total_evidences']} → Hallazgos: {result.get('n_findings', 0)}")
        writeLog("info", logger, f"  Evidencias disponibles para mostrar: {len(result.get('all_evidences', []))} (de {len(result.get('all_papers', []))} papers)")
        for i, finding in enumerate(result.get('findings', [])):
            preview = finding['finding'][:100] if finding['finding'] else "[EMPTY]"
            writeLog("info", logger, f"    Finding {i + 1}: {preview}...")
            writeLog("info", logger, f"      Papers: {finding['supporting_papers']}")


if __name__ == "__main__":
    processSynthesizeFindings()