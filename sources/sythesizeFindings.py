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
import hashlib
from functools import lru_cache

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"

# CHANGED: was llama3.1:8b for synthesis + llama3.2:3b (separate
# LLM_CONFIG) for translation — two different models loaded in the same
# module. GLM-5.2 was evaluated as an alternative but discarded: it is
# a 744B-parameter MoE model (~40B active) requiring 176-280GB even at
# aggressive 2-bit quantization — completely out of range for an RTX
# 4060 (8GB) and designed for long-horizon agentic coding, not short
# academic synthesis. qwen3:8b is unified across both tasks here,
# consistent with trainingMaterials.py and the rest of the pipeline:
# one model loaded in Ollama at a time, simpler VRAM management, and
# comparable or better performance on structured technical synthesis
# than llama3.1:8b for this use case.
LLM_MODEL = "qwen3:8b"

RELEVANCE_PERCENTILE = 0.6
FOCUS_TERM_BOOST_WEIGHT = 0.15
MAX_STRUCTURED_TEXT = 1500
MAX_EVIDENCES_PER_CLUSTER = 12
MAX_EVIDENCES_PER_CONCEPT = 30

MIN_EVIDENCE_SCORE = 0.5
MIN_FINDING_SCORE = 0.6

# CHANGED: single LLM endpoint config, used for both synthesis
# (via ollama.generate) and translation (via call_llm/requests).
# Previously LLM_CONFIG pointed at a different, smaller model than
# LLM_MODEL above.
LLM_CONFIG = {
    "backend": "ollama",
    "model": LLM_MODEL,
    "url": "http://localhost:11434/api/generate",
}

# Disk-persisted translation cache — survives across pipeline re-runs.
# CHANGED: previously only an in-memory lru_cache, reset on every
# execution. With extract_structured_evidence translating up to 4
# fields per evidence across many evidences and concepts, the cache
# hit rate across re-runs (common in an iterative research pipeline)
# matters for both time and LLM load.
TRANSLATION_CACHE_FILE = "translation_cache.json"
_translation_cache: dict = {}


# --------------------------------------------------
# FUNCIONES AUXILIARES
# --------------------------------------------------

def load_json(file_path: Path) -> dict:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def call_llm(prompt: str, max_retries: int = 2, num_predict: int = 1024) -> str:
    for attempt in range(max_retries):
        try:
            response = requests.post(
                LLM_CONFIG["url"],
                json={
                    "model": LLM_CONFIG["model"],
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.2, "num_predict": num_predict},
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


# --------------------------------------------------
# TRANSLATION CACHE (disk-backed)
# --------------------------------------------------

def _load_translation_cache(output_dir: Path) -> dict:
    cache_file = output_dir / TRANSLATION_CACHE_FILE
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_translation_cache(output_dir: Path, cache: dict) -> None:
    cache_file = output_dir / TRANSLATION_CACHE_FILE
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def translate_to_spanish(text: str) -> str:
    """
    CHANGED: now backed by a disk-persisted cache (_translation_cache)
    in addition to the original within-run deduplication. Re-running
    the pipeline no longer re-translates identical evidence text.
    """
    if not text or len(text.strip()) < 10:
        return text

    clean_text = re.sub(r'\s+', ' ', text).strip()
    key = _text_hash(clean_text)

    if key in _translation_cache:
        return _translation_cache[key]

    text_for_prompt = clean_text
    if len(text_for_prompt) > 3000:
        text_for_prompt = text_for_prompt[:3000] + "..."

    prompt = f"""Traduce el siguiente texto académico del inglés al español. Mantén el tono formal, la terminología técnica y la puntuación. Devuelve solo la traducción.

Texto original:
{text_for_prompt}

Traducción:"""
    translated = call_llm(prompt)
    result = translated if translated else text

    _translation_cache[key] = result
    return result


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

    filtered_evidences = [ev for ev in cluster["evidences"] if ev["similarity"] >= MIN_EVIDENCE_SCORE]
    if not filtered_evidences:
        return None

    top_evidences = sorted(filtered_evidences, key=lambda x: x["similarity"], reverse=True)[:MAX_EVIDENCES_PER_CLUSTER]

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

    focus_instruction = ""
    if focus_terms:
        focus_terms_str = ", ".join(focus_terms[:10])
        focus_instruction = f"""
Special instructions for this research:
- The core focus of this thesis is on: {focus_terms_str}.
- When synthesizing the finding, prioritize aspects related to these terms if they appear in the evidence.
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
        "top_quotes": top_quotes
    }


# --------------------------------------------------
# FILTRADO POR RELEVANCIA (percentil)
# --------------------------------------------------

def score_findings_relevance(
    findings: list,
    concept_query: str,
    embedding_model: SentenceTransformer,
    focus_terms: list[str] = None
) -> list[float]:
    """
    CHANGED: concept_embedding is now computed ONCE for all findings of
    a concept, instead of once per finding inside the old
    is_finding_relevant_to_concept. With 5-10 findings per concept,
    the previous version re-encoded the identical concept_query text
    5-10 times — same embedding, repeated work.
    """
    if not findings:
        return []

    concept_embedding = embedding_model.encode(concept_query, normalize_embeddings=True)

    finding_texts = [f["finding"] for f in findings]
    finding_embeddings = embedding_model.encode(
        finding_texts, normalize_embeddings=True, batch_size=16, show_progress_bar=False
    )

    scores = []
    for finding_text, finding_emb in zip(finding_texts, finding_embeddings):
        sim = float(np.dot(finding_emb, concept_embedding))
        if focus_terms:
            lower_finding = finding_text.lower()
            focus_hits = sum(1 for term in focus_terms if term.lower() in lower_finding)
            sim += focus_hits * FOCUS_TERM_BOOST_WEIGHT
        scores.append(sim)

    return scores


def filter_findings_by_relevance(
    findings: list,
    concept_query: str,
    embedding_model: SentenceTransformer,
    focus_terms: list[str] = None,
    percentile_threshold: float = RELEVANCE_PERCENTILE
) -> list:
    if not findings:
        return []

    # CHANGED: batched scoring instead of one encode-per-finding call
    similarities = score_findings_relevance(findings, concept_query, embedding_model, focus_terms)

    dynamic_threshold = np.percentile(similarities, (1 - percentile_threshold) * 100)
    writeLog("info", logger,
             f"[Filter] {len(findings)} findings, threshold={dynamic_threshold:.3f} "
             f"(percentile {percentile_threshold * 100:.0f}% kept)")
    return [f for f, sim in zip(findings, similarities) if sim >= dynamic_threshold]


# --------------------------------------------------
# SÍNTESIS POR CONCEPTO
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

    clusters_for_findings = [c for c in clusters if c["size"] >= 2]
    all_clusters = clusters

    all_evidences_dict = {}

    raw_findings = []
    for cluster in clusters_for_findings:
        finding = generate_finding_from_cluster(
            concept_id, concept_query, cluster, llm_model, focus_terms, papers_text
        )
        if finding is None:
            continue
        raw_findings.append(finding)
        for quote in finding["top_quotes"]:
            key = (str(quote["doc_id"]), str(quote["text"]))
            if key not in all_evidences_dict or quote["similarity"] > all_evidences_dict[key]["similarity"]:
                all_evidences_dict[key] = quote

    for cluster in all_clusters:
        if cluster["size"] == 1:
            ev = cluster["evidences"][0]
            if ev["similarity"] < MIN_EVIDENCE_SCORE:
                continue
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
    global _translation_cache

    input_dir, output_dir = inicioModulo("processSynthesizeFindings")
    input_file = output_dir / "clustered_evidences.json"
    if not input_file.exists():
        writeLog("error", logger, f"[Synthesis] No se encuentra {input_file}")
        return

    focus_terms = load_focus_terms(input_dir)
    papers_text = load_papers_text(output_dir)

    # NEW: load disk-persisted translation cache before any translation happens
    _translation_cache = _load_translation_cache(output_dir)
    writeLog("info", logger, f"[Synthesis] Loaded {len(_translation_cache)} cached translations")

    writeLog("info", logger, f"[Synthesis] PASS 1: Generating findings with {LLM_MODEL}...")
    data = load_clustered_evidences(input_file)

    raw_results = []
    for concept_data in data:
        result = synthesize_concept_no_filter(concept_data, LLM_MODEL, focus_terms, papers_text)
        raw_results.append(result)

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

        findings_above_min_score = [f for f in result["findings_raw"] if f["avg_evidence_score"] >= MIN_FINDING_SCORE]
        if not findings_above_min_score:
            writeLog("info", logger,
                     f"[Synthesis] Concept {result['concept_id']}: No findings with avg_score >= {MIN_FINDING_SCORE}. Discarding all.")
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
            findings_above_min_score, result["concept_query"], embedding_model,
            focus_terms, RELEVANCE_PERCENTILE
        )
        if not filtered_findings and findings_above_min_score:
            filtered_findings = [findings_above_min_score[0]]
            writeLog("info", logger, f"[Synthesis] Concept {result['concept_id']}: No findings passed percentile, keeping best one.")

        filtered_results.append({
            "concept_id": result["concept_id"],
            "concept_query": result["concept_query"],
            "total_evidences": result["total_evidences"],
            "focus_terms_used": focus_terms or [],
            "n_findings": len(filtered_findings),
            "findings": filtered_findings,
            "all_evidences": result["all_evidences"],
            "all_papers": result["all_papers"]
        })

        writeLog("info", logger,
                 f"[Synthesis] Concept {result['concept_id']}: "
                 f"{result['n_findings_raw']} generated → "
                 f"{len(findings_above_min_score)} passed MIN_FINDING_SCORE → "
                 f"{len(filtered_findings)} after percentile filtering, "
                 f"{len(result['all_evidences'])} total evidences collected from {len(result['all_papers'])} papers")

    output_file = output_dir / "concept_findings.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(filtered_results, f, indent=2, ensure_ascii=False)
    writeLog("info", logger, f"[Synthesis] Saved to {output_file}")

    # NEW: persist translation cache for future pipeline re-runs
    _save_translation_cache(output_dir, _translation_cache)
    writeLog("info", logger, f"[Synthesis] Saved {len(_translation_cache)} translations to cache")

    # Summary
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, "RESUMEN DE SÍNTESIS")
    writeLog("info", logger, "=" * 60)
    for result in filtered_results:
        writeLog("info", logger, f"Concepto {result['concept_id']}: {result['concept_query']}")
        writeLog("info", logger,
                 f"  Evidencias totales: {result['total_evidences']} → Hallazgos: {result.get('n_findings', 0)} "
                 f"| Disponibles: {len(result.get('all_evidences', []))} de {len(result.get('all_papers', []))} papers")
        for i, finding in enumerate(result.get('findings', [])):
            preview = finding['finding'][:100] if finding['finding'] else "[EMPTY]"
            writeLog("info", logger, f"    Finding {i + 1}: {preview}...")


if __name__ == "__main__":
    processSynthesizeFindings()