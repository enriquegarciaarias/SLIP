#!/usr/bin/env python3
"""
argumentativeZoning.py
======================

Argumentative Zoning (AZ) para papers científicos.
Clasifica párrafos/oraciones en movimientos retóricos:

ZONAS PRINCIPALES (basado en esquema de Teufel et al. / PubMed-RCT):
  - CLAIM: Contribución principal, hallazgo clave, tesis del paper
  - EVIDENCE: Datos, resultados, métricas, experimentos que soportan claim
  - METHOD: Metodología, arquitectura, algoritmos, setup experimental
  - BACKGROUND: Contexto, trabajo relacionado, definiciones, estado del arte
  - GAP: Limitación de trabajos previos, problema no resuelto, motivación
  - FUTURE_WORK: Trabajo futuro, extensiones, limitaciones declaradas
  - REST: Otro (acknowledgments, referencias, etc.)

Integración:
  - Se ejecuta DESPUÉS de corpusCleaning (usa clean_sections)
  - Output: papers_zoned.json con zona por párrafo
  - Usado por: conceptMiningEngine (ponderar keywords por zona), 
    conceptEvidence (priorizar EVIDENCE/CLAIM), 
    clusterEvidences (agrupar por zona)
"""

from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo, read_json, write_json
from pathlib import Path
import json
import re
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

# ============================================================
# DEFINICIÓN DE ZONAS
# ============================================================

class ZoneType(str, Enum):
    CLAIM = "claim"              # Contribución, hallazgo principal, conclusión
    EVIDENCE = "evidence"        # Datos, resultados, métricas, experimentos
    METHOD = "method"            # Metodología, arquitectura, setup
    BACKGROUND = "background"    # Contexto, related work, definiciones
    GAP = "gap"                  # Problema, limitación previa, motivación
    FUTURE_WORK = "future_work"  # Trabajo futuro, limitaciones propias
    REST = "rest"                # Acknowledgments, referencias, etc.

# Pesos por zona para downstream tasks
ZONE_WEIGHTS = {
    ZoneType.CLAIM: 2.0,
    ZoneType.EVIDENCE: 1.8,
    ZoneType.METHOD: 1.5,
    ZoneType.GAP: 1.3,
    ZoneType.FUTURE_WORK: 0.8,
    ZoneType.BACKGROUND: 0.5,
    ZoneType.REST: 0.0,
}

# Configuración de refiner LLM (solo para párrafos de baja confianza)
LLM_REFINER_ENABLED = False
LLM_REFINER_CONFIDENCE_THRESHOLD = 0.6

# Patrones lingüísticos por zona (heurísticos)
ZONE_PATTERNS = {
    ZoneType.CLAIM: [
        r"\bwe (propose|present|introduce|demonstrate|show|prove|establish)\b",
        r"\bour (approach|method|model|framework|system|contribution)\b",
        r"\bthis (paper|work|study) (presents|proposes|introduces|demonstrates)\b",
        r"\bwe (achieve|obtain|reach|improve|outperform)\b",
        r"\bthe (main|key|primary) (contribution|finding|result) is\b",
    ],
    ZoneType.EVIDENCE: [
        r"\b(results?|findings?|experiments?|evaluation)\b",
        r"\b(accuracy|f1|auc|precision|recall|sensitivity|specificity)\b",
        r"\b(table|figure|fig\.)\s*\d+\b",
        r"\b(compared to|outperforms?|surpasses?|exceeds?)\b",
        r"\b(\d+(\.\d+)?%|p\s*[<=>]\s*0\.\d+|r\s*=\s*[-]?\d+\.\d+)\b",
        r"\b(baseline|benchmark|state.of.the.art)\b",
        r"\b(we (evaluate|test|validate|measure|assess))\b",
    ],
    ZoneType.METHOD: [
        r"\b(method|methodology|approach|algorithm|architecture|model)\b",
        r"\b(we (use|employ|apply|implement|train|fine.tune|optimize))\b",
        r"\b(dataset|corpus|training|validation|testing)\b",
        r"\b(hyperparameter|parameter|configuration|setup|pipeline)\b",
        r"\b(encoder|decoder|transformer|attention|embedding|layer)\b",
        r"\b(preprocessing|tokenization|normalization|augmentation)\b",
    ],
    ZoneType.BACKGROUND: [
        r"\b(related work|previous work|prior work|existing work)\b",
        r"\b(literature|survey|review|overview|background)\b",
        r"\b(has been (shown|proven|demonstrated|studied|investigated))\b",
        r"\b(according to|as (shown|demonstrated|reported) (in|by))\b",
        r"\b(previous|earlier|existing|traditional|conventional)\s+(work|methods?|approaches?)\b",
    ],
    ZoneType.GAP: [
        r"\b(however|nevertheless|nonetheless|despite|although)\b",
        r"\b(limitation|shortcoming|drawback|weakness|gap)\b",
        r"\b(still (remains|lack|need|require)|remains (an open|a challenge))\b",
        r"\b(little (is known|attention|work)|few (studies|works|attempts))\b",
        r"\b(not (well|sufficiently|adequately|fully) (understood|addressed|studied|explored))\b",
        r"\b(still (an open problem|a challenge|unresolved|unsolved))\b",
    ],
    ZoneType.FUTURE_WORK: [
        r"\b(future work|future research|future studies|in the future)\b",
        r"\b(we (plan|intend|aim|hope|will) (to|investigate|explore|extend|improve))\b",
        r"\b(further (research|work|study|investigation|analysis) (is needed|required))\b",
        r"\b(limitation of (this|our) (work|study|paper|approach|method))\b",
        r"\b(could be (improved|extended|explored|investigated))\b",
    ],
    ZoneType.REST: [
        r"\b(acknowledgment|funding|grant|thank|support)\b",
        r"\breferences?\b",
        r"\bappendix\b",
    ],
}

# Compilar regex
COMPILED_PATTERNS = {
    zone: [re.compile(p, re.IGNORECASE) for p in patterns]
    for zone, patterns in ZONE_PATTERNS.items()
}

# Secciones que sugieren zona por defecto
SECTION_TO_ZONE = {
    "abstract": ZoneType.CLAIM,
    "introduction": ZoneType.BACKGROUND,
    "related work": ZoneType.BACKGROUND,
    "background": ZoneType.BACKGROUND,
    "methodology": ZoneType.METHOD,
    "methods": ZoneType.METHOD,
    "approach": ZoneType.METHOD,
    "method": ZoneType.METHOD,
    "results": ZoneType.EVIDENCE,
    "findings": ZoneType.EVIDENCE,
    "evaluation": ZoneType.EVIDENCE,
    "experiments": ZoneType.EVIDENCE,
    "discussion": ZoneType.CLAIM,
    "conclusion": ZoneType.CLAIM,
    "conclusions": ZoneType.CLAIM,
    "future work": ZoneType.FUTURE_WORK,
    "limitations": ZoneType.FUTURE_WORK,
    "acknowledgments": ZoneType.REST,
    "funding": ZoneType.REST,
    "references": ZoneType.REST,
    "bibliography": ZoneType.REST,
}

# ============================================================
# DATACLASS PARA RESULTADO
# ============================================================

@dataclass
class ZonedParagraph:
    paper_id: str
    paragraph_idx: int
    section: str
    text: str
    zone: ZoneType
    confidence: float
    char_start: int
    char_end: int

@dataclass
class ZonedPaper:
    paper_id: str
    title: str
    paragraphs: List[ZonedParagraph]

# ============================================================
# CLASIFICADOR HEURÍSTICO
# ============================================================

class HeuristicZoner:
    """Clasificador basado en reglas heurísticas + sección."""
    
    def __init__(self):
        self.section_zone_map = {k.lower(): v for k, v in SECTION_TO_ZONE.items()}
    
    def classify_paragraph(self, text: str, section: str) -> Tuple[ZoneType, float]:
        """Clasifica un párrafo individual."""
        text_lower = text.lower()
        section_lower = section.lower()
        
        # 1. Zona por defecto según sección
        default_zone = ZoneType.REST
        for sec_key, zone in self.section_zone_map.items():
            if sec_key in section_lower:
                default_zone = zone
                break
        
        # 2. Scoring por patrones
        scores = {zone: 0.0 for zone in ZoneType}
        
        for zone, patterns in COMPILED_PATTERNS.items():
            for pattern in patterns:
                matches = len(pattern.findall(text_lower))
                if matches > 0:
                    scores[zone] += matches * 0.3
        
        # 3. Boost por sección
        scores[default_zone] += 0.5
        
        # 4. Reglas especiales
        # - Si es introduction pero tiene "we propose" -> CLAIM
        if default_zone == ZoneType.BACKGROUND and any(p.search(text_lower) for p in COMPILED_PATTERNS[ZoneType.CLAIM]):
            scores[ZoneType.CLAIM] += 0.8
        
        # - Si es results pero tiene "we propose" -> METHOD
        if default_zone == ZoneType.EVIDENCE and any(p.search(text_lower) for p in COMPILED_PATTERNS[ZoneType.METHOD]):
            scores[ZoneType.METHOD] += 0.5
        
        # 5. Normalizar y seleccionar
        max_score = max(scores.values())
        if max_score == 0:
            return default_zone, 0.3
        
        best_zone = max(scores, key=scores.get)
        confidence = min(max_score / 2.0, 1.0)  # Normalizar aprox
        
        return best_zone, confidence
    
    def zone_paper(self, paper: dict) -> ZonedPaper:
        """Zona un paper completo."""
        paper_id = paper.get("paper_id", "unknown")
        title = paper.get("title", "")
        sections = paper.get("clean_sections", {}) or paper.get("sections", {})
        
        paragraphs = []
        para_idx = 0
        
        for section_name, section_text in sections.items():
            if not section_text or len(section_text.strip()) < 30:
                continue
            
            # Dividir en párrafos
            paras = [p.strip() for p in section_text.split("\n\n") if p.strip() and len(p.strip()) > 30]
            
            char_offset = 0
            for para in paras:
                zone, confidence = self.classify_paragraph(para, section_name)
                
                paragraphs.append(ZonedParagraph(
                    paper_id=paper_id,
                    paragraph_idx=para_idx,
                    section=section_name,
                    text=para,
                    zone=zone,
                    confidence=confidence,
                    char_start=char_offset,
                    char_end=char_offset + len(para)
                ))
                char_offset += len(para) + 2
                para_idx += 1
        
        return ZonedPaper(paper_id=paper_id, title=title, paragraphs=paragraphs)


# ============================================================
# CLASIFICADOR LLM (opcional, refiner para baja confianza)
# ============================================================

LLM_ZONING_PROMPT = """Classify the following paragraph from a scientific paper into ONE argumentative zone.

ZONES:
- claim: Main contribution, key finding, thesis, "we propose/achieve/show"
- evidence: Data, results, metrics, experiments, numbers, comparisons
- method: Methodology, architecture, algorithm, setup, model details
- background: Context, related work, definitions, prior work
- gap: Problem, limitation of prior work, motivation, "however..."
- future_work: Future work, limitations of THIS work, extensions
- rest: Acknowledgments, funding, references, appendix

PARAGRAPH:
"{text}"

SECTION: {section}

Output ONLY the zone name (one word)."""


class LLMZoner:
    """Clasificador basado en LLM para casos difíciles (refiner de baja confianza)."""
    
    def __init__(self):
        self.client = None
    
    def _get_client(self):
        if self.client is None:
            try:
                from sources.common.llm_client import create_resilient_ollama_client
                self.client = create_resilient_ollama_client(
                    temperature=0.0,
                    max_tokens=10,
                )
            except Exception as e:
                writeLog("warning", logger, f"[ArgumentativeZoning] LLM client unavailable: {e}")
                self.client = False
        return self.client
    
    def classify(self, text: str, section: str) -> Optional[Tuple[ZoneType, float]]:
        client = self._get_client()
        if not client:
            return None
        
        prompt = LLM_ZONING_PROMPT.format(text=text[:1500], section=section)
        
        try:
            response = client.generate_text(
                prompt=prompt,
                system_prompt="You are an expert in scientific discourse analysis. Classify paragraphs into argumentative zones.",
                temperature=0.0,
                max_tokens=10,
                context="argumentativeZoning.classify",
                keep_alive=0,
            )
            
            if response:
                zone_str = response.strip().lower()
                for zone in ZoneType:
                    if zone.value == zone_str:
                        return zone, 0.85
        except Exception as e:
            writeLog("debug", logger, f"[ArgumentativeZoning] LLM classification failed: {e}")
        
        return None


# ============================================================
# PIPELINE PRINCIPAL
# ============================================================

def processArgumentativeZoning(input_dir: Path = None, output_dir: Path = None):
    """
    Ejecuta Argumentative Zoning sobre papers_text.json.
    
    Input:  papers_text.json (clean_sections por paper)
    Output: papers_zoned.json (párrafos con zona asignada)
    """
    writeLog("info", logger, "🚀 [START] Processing Argumentative Zoning")
    
    if input_dir is None or output_dir is None:
        input_dir, output_dir = inicioModulo("processArgumentativeZoning")
    
    text_file = output_dir / "papers_text.json"
    if not text_file.exists():
        writeLog("error", logger, f"[AZ] Not found: {text_file}. Run corpusCleaning first.")
        return None
    
    zoned_file = output_dir / "papers_zoned.json"
    
    # Cargar papers
    with open(text_file, "r", encoding="utf-8") as f:
        papers = json.load(f)
    
    writeLog("info", logger, f"[AZ] Loaded {len(papers)} papers from papers_text.json")
    
    # Cargar existente (idempotente)
    existing = {}
    if zoned_file.exists():
        try:
            existing_data = read_json(zoned_file)
            if isinstance(existing_data, list):
                existing = {p["paper_id"]: p for p in existing_data if "paper_id" in p}
            writeLog("info", logger, f"[AZ] Loaded {len(existing)} existing zoned papers")
        except Exception as e:
            writeLog("warning", logger, f"[AZ] Could not load existing zoned file: {e}")
    
    # Clasificadores
    heuristic_zoner = HeuristicZoner()
    
    # LLM refiner para párrafos de baja confianza (opcional, configurable)
    defaults = getattr(processControl, "defaults", None) or {}
    az_cfg = defaults.get("argumentativeZoning", {}) if isinstance(defaults, dict) else {}
    llm_refiner_enabled = bool(az_cfg.get("llm_refiner_enabled", LLM_REFINER_ENABLED))
    llm_refiner_threshold = float(az_cfg.get("llm_refiner_confidence_threshold", LLM_REFINER_CONFIDENCE_THRESHOLD))
    
    llm_zoner = LLMZoner() if llm_refiner_enabled else None
    
    if llm_refiner_enabled:
        writeLog("info", logger, f"[AZ] LLM refiner enabled (threshold={llm_refiner_threshold})")
    processed = 0
    skipped = 0
    llm_used = 0
    zoned_papers = []
    
    for paper in papers:
        paper_id = paper.get("paper_id", "unknown")
        
        # Verificar si ya existe y está completo
        if paper_id in existing:
            existing_paper = existing[paper_id]
            if existing_paper.get("paragraphs") and len(existing_paper["paragraphs"]) > 0:
                zoned_papers.append(existing_paper)
                skipped += 1
                continue
        
        writeLog("info", logger, f"[AZ] Zoning {paper_id}...")
        
        # Zonificación heurística (rápida y efectiva)
        zoned_paper = heuristic_zoner.zone_paper(paper)
        
        # Refinamiento LLM para párrafos de baja confianza
        if llm_refiner_enabled and llm_zoner:
            refined_paragraphs = []
            for zp in zoned_paper.paragraphs:
                if zp.confidence < llm_refiner_threshold and len(zp.text) > 100:
                    llm_result = llm_zoner.classify(zp.text, zp.section)
                    if llm_result:
                        llm_zone, llm_conf = llm_result
                        if llm_conf > zp.confidence:
                            zp.zone = llm_zone
                            zp.confidence = llm_conf
                            llm_used += 1
                refined_paragraphs.append(zp)
            
            zoned_paper.paragraphs = refined_paragraphs
        
        # Convertir a dict serializable
        paper_dict = {
            "paper_id": zoned_paper.paper_id,
            "title": zoned_paper.title,
            "paragraphs": [
                {
                    "paragraph_idx": p.paragraph_idx,
                    "section": p.section,
                    "text": p.text,
                    "zone": p.zone.value,
                    "confidence": round(p.confidence, 3),
                    "char_start": p.char_start,
                    "char_end": p.char_end,
                }
                for p in zoned_paper.paragraphs
            ]
        }
        zoned_papers.append(paper_dict)
        processed += 1
    
    # Guardar
    write_json(zoned_file, zoned_papers)
    
    # Estadísticas
    zone_counts = {}
    for zp in zoned_papers:
        for p in zp["paragraphs"]:
            z = p["zone"]
            zone_counts[z] = zone_counts.get(z, 0) + 1
    
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, "ARGUMENTATIVE ZONING COMPLETED")
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, f"  Papers totales:      {len(papers)}")
    writeLog("info", logger, f"  Procesados nuevos:   {processed}")
    writeLog("info", logger, f"  Ya existentes (skip): {skipped}")
    if llm_refiner_enabled:
        writeLog("info", logger, f"  LLM refiner usado:   {llm_used} párrafos")
    writeLog("info", logger, f"  Output:              {zoned_file}")
    writeLog("info", logger, f"  Distribución zonas:")
    for zone, count in sorted(zone_counts.items(), key=lambda x: -x[1]):
        writeLog("info", logger, f"    {zone}: {count}")
    writeLog("info", logger, "✅ [END] Argumentative Zoning")
    
    return zoned_papers


def load_zoned_papers(output_dir: Path) -> Dict[str, dict]:
    """Carga papers_zoned.json y retorna dict paper_id -> zoned_paper."""
    zoned_file = output_dir / "papers_zoned.json"
    if not zoned_file.exists():
        return {}
    try:
        with open(zoned_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {p.get("paper_id"): p for p in data if p.get("paper_id")}
    except Exception as e:
        writeLog("warning", logger, f"[AZ] Could not load papers_zoned.json: {e}")
    return {}


def get_paragraphs_by_zone(zoned_paper: dict, zones: List[ZoneType]) -> List[dict]:
    """Filtra párrafos de un paper por zona(s)."""
    return [
        p for p in zoned_paper.get("paragraphs", [])
        if ZoneType(p["zone"]) in zones
    ]


def get_claim_evidence_pairs(zoned_paper: dict) -> List[Tuple[dict, dict]]:
    """Extrae pares (claim, evidence) de un paper zonificado."""
    claims = get_paragraphs_by_zone(zoned_paper, [ZoneType.CLAIM])
    evidence = get_paragraphs_by_zone(zoned_paper, [ZoneType.EVIDENCE])
    
    pairs = []
    for claim in claims:
        # Asociar evidence más cercano (heurística simple)
        for ev in evidence:
            pairs.append((claim, ev))
    return pairs


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
    
    processArgumentativeZoning()