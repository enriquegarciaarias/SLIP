#!/usr/bin/env python3
"""
structureExtraction.py
======================

Extracción estructurada de información clave de papers científicos usando LLM.
Se ejecuta DESPUÉS de corpusCleaning y ANTES de discoveryEngine.

Input:  papers_text.json (clean_text + clean_sections por paper)
Output: papers_structured.json (información estructurada por paper)

Campos extraídos:
  - rq_explicit: preguntas de investigación explícitas
  - hypotheses: hipótesis planteadas
  - contributions: contribuciones declaradas
  - methodology: {approach, architecture, datasets, baselines, metrics, signal_modalities}
  - results: {main, ablation, key_numbers, tables_referenced}
  - limitations: limitaciones declaradas
  - future_work: trabajo futuro sugerido
  - key_figures: captions de figuras/ tablas relevantes
  - takeaway: mensaje principal en 1 frase
"""

from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo, read_json, write_json
from pathlib import Path
import json
import time
import hashlib
from typing import Dict, List, Optional, Any

# ============================================================
# PROMPT DE EXTRACCIÓN ESTRUCTURADA
# ============================================================

SYSTEM_PROMPT = """You are an expert scientific literature analyst. Extract structured information from the provided paper text. Be precise, concise, and faithful to the paper. Do not hallucinate. If information is not present, return empty list/string."""

EXTRACTION_PROMPT = """Extract the following structured information from this scientific paper. Return ONLY valid JSON with the exact schema below.

PAPER TEXT (title, abstract, sections):
---
{text}
---

SCHEMA (return exactly these keys):
{{
  "rq_explicit": [],                    // List of explicit research questions stated in the paper
  "hypotheses": [],                     // List of hypotheses tested or proposed
  "contributions": [],                  // List of claimed contributions (novelty)
  "methodology": {{
    "approach": "",                     // High-level approach (e.g., "LLM-based framework with medically-informed prompts")
    "architecture": "",                 // Model architecture details (e.g., "GPT-4o mini with LoRA fine-tuning")
    "datasets": [],                     // Datasets used (e.g., ["MERSA", "WESAD"])
    "baselines": [],                    // Baseline methods compared against
    "metrics": [],                      // Evaluation metrics (e.g., ["accuracy", "F1", "AUC"])
    "signal_modalities": []             // Physiological signals used (e.g., ["EEG", "ECG", "HRV", "EMG"])
  }},
  "results": {{
    "main": "",                         // Main quantitative result (e.g., "96.3% accuracy, outperforms baselines by 12%")
    "ablation": [],                     // Key ablation findings
    "key_numbers": [],                  // Important numbers (e.g., ["217 participants", "1,391 samples", "F1=0.761"])
    "tables_referenced": []             // Tables/figures with key results (e.g., ["Table 2", "Fig. 3"])
  }},
  "limitations": [],                    // Stated limitations
  "future_work": [],                    // Suggested future work
  "key_figures": [],                    // Important figure/table captions or descriptions
  "takeaway": ""                        // One-sentence core message
}}

RULES:
- Use paper's own terminology
- Be specific (model names, dataset names, metric values)
- Empty string/list if not found
- No markdown, no extra commentary
- Valid JSON only
"""

# ============================================================
# CONFIG
# ============================================================

MAX_TEXT_CHARS = 12000  # Límite de texto enviado al LLM por paper
LLM_TEMPERATURE = 0.0
LLM_MAX_TOKENS = 2000

# Cache LLM persistente cross-runs (evita re-llamar al LLM para el mismo texto)
LLM_CACHE_ENABLED = True
LLM_CACHE_FILE = "llm_cache.json"

# ============================================================
# FUNCIONES AUXILIARES
# ============================================================

def build_paper_text(paper: Dict) -> str:
    """Construye texto combinado para el LLM (title + abstract + secciones clave)."""
    parts = []
    
    # Title
    title = paper.get("title", "").strip()
    if title:
        parts.append(f"TITLE: {title}")
    
    # Abstract (desde clean_sections o campo directo)
    sections = paper.get("clean_sections", {}) or paper.get("sections", {})
    abstract = sections.get("abstract", "").strip()
    if not abstract:
        abstract = paper.get("abstract", "").strip()
    if abstract:
        parts.append(f"ABSTRACT: {abstract[:3000]}")
    
    # Secciones prioritarias
    priority_sections = ["methodology", "methods", "results", "findings", "conclusion", "discussion", "introduction"]
    for sec in priority_sections:
        sec_text = sections.get(sec, "").strip()
        if sec_text and len(sec_text) > 100:
            parts.append(f"{sec.upper()}: {sec_text[:4000]}")
    
    # Fallback: clean_text si no hay secciones
    if len(parts) <= 2:
        clean_text = paper.get("clean_text", "").strip()
        if clean_text:
            parts.append(f"FULL_TEXT: {clean_text[:8000]}")
    
    combined = "\n\n".join(parts)
    return combined[:MAX_TEXT_CHARS]


    combined = "\n\n".join(parts)
    return combined[:MAX_TEXT_CHARS]


# ============================================================
# CACHE LLM PERSISTENTE (evita re-llamadas al LLM entre runs)
# ============================================================

_llm_cache: Dict[str, Dict] = {}
_llm_cache_dirty = False


def _llm_cache_key(text: str, paper_id: str) -> str:
    """Genera una clave única basada en el texto y paper_id."""
    combined = f"{paper_id}::{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
    return combined


def _load_llm_cache(output_dir: Path) -> Dict[str, Dict]:
    """Carga el cache LLM desde disco si existe."""
    cache_file = output_dir / LLM_CACHE_FILE
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            writeLog("warning", logger,
                     f"[StructureExtraction] Error loading LLM cache: {e}")
    return {}


def _save_llm_cache(output_dir: Path, cache: Dict[str, Dict]) -> None:
    """Guarda el cache LLM en disco."""
    cache_file = output_dir / LLM_CACHE_FILE
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        writeLog("error", logger,
                 f"[StructureExtraction] Error saving LLM cache: {e}")


def _get_cached_llm_response(cache: Dict[str, Dict], key: str) -> Optional[Dict]:
    """Recupera una respuesta cached del LLM si existe."""
    if key in cache:
        entry = cache[key]
        return entry.get("response")
    return None


def _save_cached_llm_response(cache: Dict[str, Dict], key: str, response: Dict) -> None:
    """Guarda una respuesta del LLM en el cache."""
    cache[key] = {
        "response": response,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def call_llm_for_extraction(text: str, paper_id: str) -> Optional[Dict]:
    """Llama al LLM para extraer estructura. Retorna dict o None si falla."""
    try:
        from sources.common.llm_client import create_resilient_ollama_client
        llm = create_resilient_ollama_client(
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
        )
    except Exception as exc:
        writeLog("error", logger, f"[StructureExtraction] LLM client init failed for {paper_id}: {exc}")
        return None

    prompt = EXTRACTION_PROMPT.format(text=text)
    
    # ── Verificar cache LLM antes de llamar ──
    cache_key = _llm_cache_key(text, paper_id)
    cached_response = _get_cached_llm_response(_llm_cache, cache_key)
    if cached_response is not None:
        writeLog("debug", logger,
                 f"[StructureExtraction] Cache HIT for {paper_id}")
        return cached_response

    try:
        response = llm.generate_text(
            prompt=prompt,
            system_prompt=SYSTEM_PROMPT,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
            context=f"structureExtraction.{paper_id}",
            keep_alive=0,
        )
        
        if not response or not response.strip():
            writeLog("warning", logger, f"[StructureExtraction] Empty response for {paper_id}")
            return None
        
        # Limpiar respuesta (a veces el LLM añade markdown ```json ... ```)
        cleaned = response.strip()
        if cleaned.startswith("```"):
            # Extraer contenido entre backticks
            lines = cleaned.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines)
        
        parsed = json.loads(cleaned)
        
        # Validar estructura mínima
        required_keys = ["rq_explicit", "hypotheses", "contributions", "methodology", "results", "limitations", "future_work", "key_figures", "takeaway"]
        for k in required_keys:
            if k not in parsed:
                parsed[k] = [] if k != "takeaway" else ""
        
        # Validar sub-objetos
        if "methodology" not in parsed or not isinstance(parsed["methodology"], dict):
            parsed["methodology"] = {"approach": "", "architecture": "", "datasets": [], "baselines": [], "metrics": [], "signal_modalities": []}
        if "results" not in parsed or not isinstance(parsed["results"], dict):
            parsed["results"] = {"main": "", "ablation": [], "key_numbers": [], "tables_referenced": []}
        
        # ── Guardar en cache LLM ──
        if LLM_CACHE_ENABLED:
            _save_cached_llm_response(_llm_cache, cache_key, parsed)
            global _llm_cache_dirty
            _llm_cache_dirty = True
        
        return parsed
        
    except json.JSONDecodeError as e:
        writeLog("error", logger, f"[StructureExtraction] JSON parse failed for {paper_id}: {e}")
        writeLog("debug", logger, f"[StructureExtraction] Raw response: {response[:500] if 'response' in locals() else 'N/A'}")
        return None
    except Exception as e:
        writeLog("error", logger, f"[StructureExtraction] LLM call failed for {paper_id}: {e}")
        return None


def merge_with_existing(existing: Dict, extracted: Dict) -> Dict:
    """Fusiona extracción nueva con existente (idempotente: no pisa campos ya presentes)."""
    merged = dict(existing)
    for key, value in extracted.items():
        if key not in merged or not merged[key]:
            merged[key] = value
        elif key in ("methodology", "results") and isinstance(value, dict):
            # Merge profundo para objetos
            for subk, subv in value.items():
                if subk not in merged[key] or not merged[key][subk]:
                    merged[key][subk] = subv
    return merged


# ============================================================
# MAIN
# ============================================================

def processStructureExtraction(input_dir: Path = None, output_dir: Path = None):
    """
    Extrae información estructurada de papers usando LLM.
    
    Input:  output_dir / "papers_text.json"
    Output: output_dir / "papers_structured.json"
    """
    writeLog("info", logger, "🚀 [START] Processing processStructureExtraction")
    
    if input_dir is None or output_dir is None:
        input_dir, output_dir = inicioModulo("processStructureExtraction")
    
    text_file = output_dir / "papers_text.json"
    if not text_file.exists():
        writeLog("error", logger, f"[StructureExtraction] Not found: {text_file}. Run corpusCleaning first.")
        return None
    
    structured_file = output_dir / "papers_structured.json"
    
    # Cargar papers
    with open(text_file, "r", encoding="utf-8") as f:
        papers = json.load(f)
    
    writeLog("info", logger, f"[StructureExtraction] Loaded {len(papers)} papers from papers_text.json")
    
    # Leer configuración de cache LLM
    defaults = getattr(processControl, "defaults", None) or {}
    llm_cache_cfg = defaults.get("structureExtraction", {}) if isinstance(defaults, dict) else {}
    llm_cache_enabled = bool(llm_cache_cfg.get("llm_cache_enabled", LLM_CACHE_ENABLED))
    
    global _llm_cache, _llm_cache_dirty
    _llm_cache = {}
    if llm_cache_enabled:
        _llm_cache = _load_llm_cache(output_dir)
        writeLog("info", logger,
                 f"[StructureExtraction] LLM cache loaded: {len(_llm_cache)} entries")
    
    # Cargar estructurado existente (para idempotencia)
    existing_structured = {}
    if structured_file.exists():
        try:
            existing_data = read_json(structured_file)
            if isinstance(existing_data, list):
                existing_structured = {p.get("paper_id"): p for p in existing_data if p.get("paper_id")}
            writeLog("info", logger, f"[StructureExtraction] Loaded {len(existing_structured)} existing structured entries")
        except Exception as e:
            writeLog("warning", logger, f"[StructureExtraction] Could not load existing structured file: {e}")
    
    # Procesar cada paper
    structured_papers = []
    processed = 0
    skipped = 0
    failed = 0
    
    for paper in papers:
        paper_id = paper.get("paper_id", "unknown")
        
        # Verificar si ya existe y está completo
        existing = existing_structured.get(paper_id, {})
        has_content = any(existing.get(k) for k in ["contributions", "methodology", "results", "takeaway"])
        
        if has_content:
            # Ya procesado, conservar
            structured_papers.append(existing)
            skipped += 1
            continue
        
        writeLog("info", logger, f"[StructureExtraction] Processing {paper_id}...")
        
        # Construir texto
        text = build_paper_text(paper)
        if not text or len(text) < 500:
            writeLog("warning", logger, f"[StructureExtraction] Insufficient text for {paper_id}, skipping")
            failed += 1
            # Guardar entrada mínima
            structured_papers.append({"paper_id": paper_id, "skipped": True, "reason": "insufficient_text"})
            continue
        
        # Llamar LLM
        extracted = call_llm_for_extraction(text, paper_id)
        
        if extracted:
            # Fusionar con existente
            merged = merge_with_existing(existing, extracted)
            merged["paper_id"] = paper_id
            merged["extracted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            structured_papers.append(merged)
            processed += 1
            writeLog("info", logger, f"[StructureExtraction] ✓ {paper_id}: extracted {len(extracted.get('contributions', []))} contributions, {len(extracted.get('methodology', {}).get('datasets', []))} datasets")
        else:
            # Fallback: guardar entrada mínima
            structured_papers.append({"paper_id": paper_id, "skipped": True, "reason": "llm_failed"})
            failed += 1
        
        # Rate limiting suave
        time.sleep(0.1)
    
    # Guardar
    write_json(structured_file, structured_papers)
    
    # Guardar cache LLM si está habilitado y hubo cambios
    if llm_cache_enabled and _llm_cache_dirty:
        _save_llm_cache(output_dir, _llm_cache)
        writeLog("info", logger,
                 f"[StructureExtraction] LLM cache saved: {len(_llm_cache)} entries")
    
    # Resumen
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, "STRUCTURE EXTRACTION COMPLETED")
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, f"  Papers totales:      {len(papers)}")
    writeLog("info", logger, f"  Procesados nuevos:   {processed}")
    writeLog("info", logger, f"  Ya existentes (skip): {skipped}")
    writeLog("info", logger, f"  Fallidos:            {failed}")
    writeLog("info", logger, f"  Output:              {structured_file}")
    writeLog("info", logger, "✅ [END] processStructureExtraction")
    
    return structured_papers


if __name__ == "__main__":
    import argparse
    import os
    from sources.common.utils import configLoader
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", type=str, default="SensoresACL")
    args = parser.parse_args()
    
    config = configLoader()
    environment = config.get_environment()
    env = {k: (v if "realPath" in k else os.path.join(environment["realPath"], v)) for k, v in environment.items()}
    processControl.env = env
    processControl.defaults = config.get_defaults()
    processControl.args = args
    
    processStructureExtraction()