"""
synthesize_findings.py
=======================
Síntesis de hallazgos académicos por concepto a partir de evidencias
ENRIQUECIDAS (con structured_evidence ya extraído).

TODOS los datos intermedios (JSON) se generan en INGLÉS (canónico).
La traducción al español se aplica exclusivamente en la capa de renderizado final.

DEPENDENCIA: Este módulo requiere que se ejecute PRIMERO enrich_evidences.py
             y que exista enriched_evidences.json
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Set

import numpy as np
from sentence_transformers import SentenceTransformer

from sources.common.common import logger, writeLog, processControl
from sources.common.llm_client import LLMClient, create_resilient_ollama_client
from sources.common.utils import inicioModulo, read_json, write_json


# ---------------------------------------------------------------------------
# Configuración tipada
# ---------------------------------------------------------------------------

@dataclass
class SynthesisConfig:
    """Configuración completa del módulo de síntesis."""
    llm_model: str = "qwen3:8b"
    embedding_model_name: str = "BAAI/bge-base-en-v1.5"

    # Scoring y filtrado
    relevance_percentile: float = 0.6
    focus_term_boost: float = 0.15
    min_evidence_score: float = 0.5
    min_finding_score: float = 0.6
    max_evidences_per_cluster: int = 12
    max_evidences_per_concept: int = 30

    # LLM
    finding_max_tokens: int = 800
    finding_temperature: float = 0.3

    # Estilo del hallazgo: "brief", "structured" o "narrative"
    finding_style: str = "structured"

    # Diversidad y división de clusters
    max_cluster_size_for_split: Optional[int] = 10
    min_singleton_score: Optional[float] = 0.75
    diversity_penalty: float = 0.20

    # Nueva opción: incluir resumen estructurado en el prompt
    include_structured_context: bool = True

    @classmethod
    def from_project_config(cls, project: Dict) -> "SynthesisConfig":
        s = project.get("synthesis", {})
        return cls(
            llm_model=s.get("llm_model", "qwen3:8b"),
            embedding_model_name=s.get("embedding_model", "BAAI/bge-base-en-v1.5"),
            relevance_percentile=s.get("relevance_percentile", 0.6),
            focus_term_boost=s.get("focus_term_boost", 0.15),
            min_evidence_score=s.get("min_evidence_score", 0.5),
            min_finding_score=s.get("min_finding_score", 0.6),
            max_evidences_per_cluster=s.get("max_evidences_per_cluster", 12),
            finding_max_tokens=s.get("finding_max_tokens", 800),
            finding_temperature=s.get("finding_temperature", 0.3),
            finding_style=s.get("finding_style", "structured"),
            max_cluster_size_for_split=s.get("max_cluster_size_for_split", 10),
            min_singleton_score=s.get("min_singleton_score", 0.75),
            diversity_penalty=s.get("diversity_penalty", 0.20),
            include_structured_context=s.get("include_structured_context", True),
        )

    @classmethod
    def from_file(cls, study_file: Path) -> "SynthesisConfig":
        if not study_file.exists():
            writeLog("warning", logger,
                     f"[SynthesisConfig] studyDescription.json not found: {study_file}. Using defaults.")
            return cls()
        with open(study_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_project_config(data.get("project", {}))


# ---------------------------------------------------------------------------
# Objetos de dominio (inmutables)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FindingSections:
    """
    Secciones estructuradas de un hallazgo (estilo 'structured').
    El contenido está en INGLÉS (canónico).
    """
    contribucion: str = ""
    metodologia: str = ""
    resultados: str = ""
    implicaciones: str = ""
    narrative: str = ""

    @property
    def is_structured(self) -> bool:
        return bool(self.contribucion or self.metodologia
                    or self.resultados or self.implicaciones)

    def to_plain_text(self) -> str:
        """Texto plano sin headers — para embeddings y comparación semántica."""
        if self.is_structured:
            parts = [p for p in [
                self.contribucion, self.metodologia,
                self.resultados, self.implicaciones,
            ] if p]
            return " ".join(parts)
        return self.narrative

    def to_markdown(self) -> str:
        """Texto con headers Markdown (en inglés) — para renderizado."""
        if not self.is_structured:
            return self.narrative
        parts = []
        if self.contribucion:
            parts.append(f"**Main contribution:** {self.contribucion}")
        if self.metodologia:
            parts.append(f"**Methodology:** {self.metodologia}")
        if self.resultados:
            parts.append(f"**Key results:** {self.resultados}")
        if self.implicaciones:
            parts.append(f"**Implications:** {self.implicaciones}")
        return "\n\n".join(parts)

    def to_dict(self) -> Dict:
        """Serialización al JSON: texto limpio por sección."""
        if self.is_structured:
            return {
                "contribucion": self.contribucion,
                "metodologia": self.metodologia,
                "resultados": self.resultados,
                "implicaciones": self.implicaciones,
            }
        return {"narrative": self.narrative}


@dataclass(frozen=True)
class RawFinding:
    """
    Hallazgo generado por el LLM para un cluster de evidencias.
    El contenido (sections) está en INGLÉS.
    """
    sections: FindingSections
    supporting_papers: Tuple[str, ...]
    evidence_count: int
    avg_evidence_score: float
    top_quotes: Tuple[Dict, ...]
    # NUEVO: resumen estructurado agregado de las evidencias
    structured_summary: Dict[str, Any] = field(default_factory=dict)

    @property
    def finding_text(self) -> str:
        """Texto plano sin headers."""
        return self.sections.to_plain_text()

    def to_dict(self) -> Dict:
        """Serialización al JSON de salida (todo en inglés)."""
        return {
            "finding": self.finding_text,
            "finding_structured": self.sections.to_dict(),
            "finding_markdown": self.sections.to_markdown(),
            "supporting_papers": list(self.supporting_papers),
            "evidence_count": self.evidence_count,
            "avg_evidence_score": self.avg_evidence_score,
            "top_quotes": list(self.top_quotes),
            "structured_summary": self.structured_summary,
        }


@dataclass(frozen=True)
class ConceptSynthesis:
    """Resultado final de síntesis para un concepto (en inglés)."""
    concept_id: str
    concept_query: str
    total_evidences: int
    focus_terms_used: Tuple[str, ...]
    findings: Tuple[RawFinding, ...]
    all_evidences: Tuple[Dict, ...]
    all_papers: Tuple[str, ...]
    # NUEVO: agregado global de todos los hallazgos
    aggregated_summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "concept_id": self.concept_id,
            "concept_query": self.concept_query,
            "total_evidences": self.total_evidences,
            "focus_terms_used": list(self.focus_terms_used),
            "n_findings": len(self.findings),
            "findings": [f.to_dict() for f in self.findings],
            "all_evidences": list(self.all_evidences),
            "all_papers": list(self.all_papers),
            "aggregated_summary": self.aggregated_summary,
        }


# ---------------------------------------------------------------------------
# Capa 1 – Registro de modelos (embedding)
# ---------------------------------------------------------------------------

class ModelRegistry:
    """Gestiona el ciclo de vida del modelo de embeddings."""

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model: Optional[SentenceTransformer] = None

    def load(self) -> None:
        if self._model is None:
            writeLog("info", logger,
                     f"[ModelRegistry] Loading embedding model: {self._model_name}")
            self._model = SentenceTransformer(self._model_name)

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def model(self) -> SentenceTransformer:
        if self._model is None:
            raise RuntimeError("ModelRegistry.load() must be called first")
        return self._model

    def encode_batch(self, texts: List[str], normalize: bool = True) -> np.ndarray:
        return self.model.encode(
            texts,
            normalize_embeddings=normalize,
            batch_size=32,
            show_progress_bar=False,
        )


# ---------------------------------------------------------------------------
# Capa 2 – Generador de hallazgos (SIN extracción, solo síntesis)
# ---------------------------------------------------------------------------

class FindingGenerator:
    """
    Genera un hallazgo textual para un cluster usando el LLM.

    NOTA: Las evidencias ya deben venir con 'structured_evidence' pre-extraído
          por el módulo enrich_evidences.py
    """

    _SYSTEM_PROMPT = (
        "You are an expert academic researcher writing a systematic literature review. "
        "Synthesize the provided evidence snippets into a finding. "
        "Base your response EXCLUSIVELY on the provided evidence. "
        "Do not add external knowledge, citations not present in the evidence, "
        "or speculation. "
        "Respond in English, keeping technical terms in their standard form. "
        "Do NOT include any reasoning, preamble, or meta-commentary. "
        "Start your response directly with the finding content."
    )

    _OUTPUT_PREFIXES = (
        "Summary:", "summary:", "Resumen:", "resumen:",
        "Hallazgo:", "Finding:", "Síntesis:", "Respuesta:",
    )

    def __init__(self, llm_client: LLMClient, config: SynthesisConfig) -> None:
        self._llm = llm_client
        self._cfg = config

    def generate(
        self,
        concept_query: str,
        cluster: Dict,
        focus_terms: List[str],
        force_style: Optional[str] = None,
    ) -> Optional[RawFinding]:
        """
        Genera un RawFinding para un cluster.

        Args:
            concept_query: La pregunta de investigación del concepto
            cluster: Datos del cluster (con evidencias YA ENRIQUECIDAS)
            focus_terms: Términos de foco de la investigación
            force_style: Forzar un estilo específico de salida

        Returns:
            RawFinding o None si el cluster no supera el umbral
        """
        evidences = cluster.get("evidences", [])
        filtered = [e for e in evidences
                    if e.get("similarity", 0) >= self._cfg.min_evidence_score]
        if not filtered:
            return None

        top_evs = sorted(filtered, key=lambda e: e["similarity"], reverse=True)
        top_evs = top_evs[: self._cfg.max_evidences_per_cluster]

        # Los top_quotes ya vienen con structured_evidence del módulo anterior
        top_quotes = []
        for ev in top_evs:
            top_quotes.append({
                "text": ev["text"],
                "doc_id": ev["doc_id"],
                "paper_title": ev.get("paper_title", "Unknown"),
                "similarity": ev["similarity"],
                "structured_evidence": ev.get("structured_evidence", {}),
            })

        style = force_style or self._cfg.finding_style
        prompt = self._build_prompt(concept_query, cluster, evidences, focus_terms, style)
        raw_text = self._llm.generate_text(
            prompt=prompt,
            system_prompt=self._SYSTEM_PROMPT,
            temperature=self._cfg.finding_temperature,
            max_tokens=self._cfg.finding_max_tokens,
            context=f"finding:concept={concept_query[:60]}",
        )

        sections = self._parse_output(raw_text, style)

        if not sections.to_plain_text():
            ev_preview = evidences[0]["text"][:200] if evidences else ""
            sections = FindingSections(
                narrative=f"Evidence cluster ({cluster['size']} items) suggests: {ev_preview}..."
            )

        # NUEVO: agregar resumen estructurado de las evidencias
        structured_summary = self._aggregate_structured_fields(top_quotes)

        return RawFinding(
            sections=sections,
            supporting_papers=tuple(set(e["doc_id"] for e in evidences)),
            evidence_count=cluster["size"],
            avg_evidence_score=cluster["avg_evidence_score"],
            top_quotes=tuple(top_quotes),
            structured_summary=structured_summary,
        )

    # ------------------------------------------------------------------
    # Agregación de campos estructurados
    # ------------------------------------------------------------------

    def _aggregate_structured_fields(self, top_quotes: List[Dict]) -> Dict[str, Any]:
        """
        Agrega los campos estructurados de las evidencias (senales, modelos, métricas, etc.)
        """
        signals: Set[str] = set()
        models: Set[str] = set()
        metrics: Dict[str, str] = {}
        limitations: List[str] = []
        applications: List[str] = []

        for q in top_quotes:
            se = q.get("structured_evidence", {})
            for s in se.get("senales", []):
                signals.add(s)
            for m in se.get("modelos", []):
                models.add(m)
            if "metricas" in se and isinstance(se["metricas"], dict):
                metrics.update(se["metricas"])
            if se.get("limitaciones"):
                limitations.append(se["limitaciones"])
            if se.get("aplicacion"):
                applications.append(se["aplicacion"])

        # Ordenar para consistencia
        return {
            "signals": sorted(signals),
            "models": sorted(models),
            "metrics": metrics,
            "limitations": "; ".join(limitations) if limitations else "",
            "application": "; ".join(applications) if applications else "",
        }

    @staticmethod
    def _aggregate_concept_summary(findings: Tuple[RawFinding, ...]) -> Dict[str, Any]:
        """
        Agrega los resúmenes de todos los hallazgos de un concepto.
        """
        all_signals: Set[str] = set()
        all_models: Set[str] = set()
        all_metrics: Dict[str, str] = {}
        all_limitations: List[str] = []
        all_applications: List[str] = []

        for f in findings:
            summary = f.structured_summary
            for s in summary.get("signals", []):
                all_signals.add(s)
            for m in summary.get("models", []):
                all_models.add(m)
            if "metrics" in summary and isinstance(summary["metrics"], dict):
                all_metrics.update(summary["metrics"])
            if summary.get("limitations"):
                all_limitations.append(summary["limitations"])
            if summary.get("application"):
                all_applications.append(summary["application"])

        return {
            "signals": sorted(all_signals),
            "models": sorted(all_models),
            "metrics": all_metrics,
            "limitations": "; ".join(all_limitations) if all_limitations else "",
            "application": "; ".join(all_applications) if all_applications else "",
        }

    # ------------------------------------------------------------------
    # Construcción del prompt
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        concept_query: str,
        cluster: Dict,
        evidences: List[Dict],
        focus_terms: List[str],
        style: Optional[str] = None,
    ) -> str:
        style = style or self._cfg.finding_style
        # Obtener un resumen estructurado preliminar para incluir en el prompt
        top_evs = sorted(evidences, key=lambda e: e["similarity"], reverse=True)[:self._cfg.max_evidences_per_cluster]
        structured_ctx = ""
        if self._cfg.include_structured_context and top_evs:
            # Usamos las primeras evidencias para armar un bloque de contexto estructurado
            signals_summary = self._extract_common_signals(top_evs)
            models_summary = self._extract_common_models(top_evs)
            if signals_summary or models_summary:
                structured_ctx = (
                    f"\nCommon signals identified in evidence: {', '.join(signals_summary) if signals_summary else 'N/A'}.\n"
                    f"Common models identified in evidence: {', '.join(models_summary) if models_summary else 'N/A'}.\n"
                    f"Consider these when synthesizing the finding.\n"
                )

        if style == "structured":
            return self._prompt_structured(concept_query, cluster, evidences, focus_terms, structured_ctx)
        elif style == "narrative":
            return self._prompt_narrative(concept_query, cluster, evidences, focus_terms, structured_ctx)
        else:
            return self._prompt_brief(concept_query, cluster, evidences, focus_terms, structured_ctx)

    def _extract_common_signals(self, evidences: List[Dict]) -> List[str]:
        """Extrae señales comunes de las evidencias (para el prompt)."""
        all_s = set()
        for ev in evidences:
            se = ev.get("structured_evidence", {})
            for s in se.get("senales", []):
                all_s.add(s)
        return sorted(all_s)[:10]  # limitar

    def _extract_common_models(self, evidences: List[Dict]) -> List[str]:
        """Extrae modelos comunes de las evidencias."""
        all_m = set()
        for ev in evidences:
            se = ev.get("structured_evidence", {})
            for m in se.get("modelos", []):
                all_m.add(m)
        return sorted(all_m)[:10]

    def _focus_block(self, focus_terms: List[str]) -> str:
        if not focus_terms:
            return ""
        terms_str = ", ".join(focus_terms[:10])
        return (
            f"\nResearch focus (prioritize if present in evidence): {terms_str}.\n"
            f"If not mentioned in the evidence, summarize without forcing these terms.\n"
        )

    def _evidence_block(self, evidences: List[Dict]) -> str:
        return "\n".join(f"[{i+1}] {ev['text'][:600]}"
                         for i, ev in enumerate(evidences))

    def _prompt_structured(
        self,
        concept_query: str,
        cluster: Dict,
        evidences: List[Dict],
        focus_terms: List[str],
        structured_ctx: str = "",
    ) -> str:
        return (
            f"You are synthesizing evidence for a systematic literature review.\n"
            f"Research question: {concept_query}\n"
            f"{self._focus_block(focus_terms)}"
            f"{structured_ctx}\n"
            f"Evidence from {cluster['size']} sources "
            f"(avg relevance: {cluster['avg_evidence_score']:.2f}):\n\n"
            f"{self._evidence_block(evidences)}\n\n"
            f"Based ONLY on the evidence above, write a structured finding in English "
            f"with exactly these four sections. Use the exact headers shown:\n\n"
            f"**Main contribution:** [1-2 sentences about the main contribution]\n\n"
            f"**Methodology:** [1-2 sentences on methodology/approach]\n\n"
            f"**Key results:** [2-3 sentences on concrete results, metrics, comparisons]\n\n"
            f"**Implications:** [1-2 sentences on applications, gaps, limitations]\n\n"
            f"Rules: use only information from the evidence. "
            f"Keep technical terms in English. "
            f"If a section has no supporting evidence, write 'Not enough information provided.' "
            f"Do not add preamble or meta-commentary.\n\n"
            f"**Main contribution:**"
        )

    def _prompt_narrative(
        self,
        concept_query: str,
        cluster: Dict,
        evidences: List[Dict],
        focus_terms: List[str],
        structured_ctx: str = "",
    ) -> str:
        return (
            f"You are synthesizing evidence for a systematic literature review.\n"
            f"Research question: {concept_query}\n"
            f"{self._focus_block(focus_terms)}"
            f"{structured_ctx}\n"
            f"Evidence from {cluster['size']} sources "
            f"(avg relevance: {cluster['avg_evidence_score']:.2f}):\n\n"
            f"{self._evidence_block(evidences)}\n\n"
            f"Based ONLY on the evidence above, write a cohesive paragraph in English "
            f"of approximately 180-220 words that synthesizes: the main contribution, "
            f"the methodological approach, the key results, and the practical implications.\n"
            f"Keep technical terms in English. "
            f"Do not add preamble or meta-commentary. Start directly with the synthesis.\n\n"
            f"Synthesis:"
        )

    def _prompt_brief(
        self,
        concept_query: str,
        cluster: Dict,
        evidences: List[Dict],
        focus_terms: List[str],
        structured_ctx: str = "",
    ) -> str:
        return (
            f"Answer the following research question based ONLY on the evidence provided.\n"
            f"Do not add external knowledge or speculation.\n"
            f"{self._focus_block(focus_terms)}"
            f"{structured_ctx}\n"
            f"Research question: {concept_query}\n\n"
            f"Supporting evidences: {cluster['size']} "
            f"(avg quality: {cluster['avg_evidence_score']:.2f})\n\n"
            f"Evidence snippets:\n{self._evidence_block(evidences)}\n\n"
            f"Write a concise summary (2-4 sentences) in English capturing the main contribution, "
            f"key findings, and practical implications.\n\nSummary:"
        )

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_structured_output(self, text: str) -> FindingSections:
        section_headers = [
            ("contribucion",  r"\*\*Main contribution:\*\*",
             r"^main contribution\s*:?\s*"),
            ("metodologia",   r"\*\*Methodology:\*\*",
             r"^methodology\s*:?\s*"),
            ("resultados",    r"\*\*Key results:\*\*",
             r"^key results\s*:?\s*"),
            ("implicaciones", r"\*\*Implications:\*\*",
             r"^implications\s*:?\s*"),
        ]

        full_text = "**Main contribution:** " + text
        sections: Dict[str, str] = {}

        for i, (key, pattern, label_pattern) in enumerate(section_headers):
            start_match = re.search(pattern, full_text, re.IGNORECASE)
            if not start_match:
                continue
            content_start = start_match.end()
            if i + 1 < len(section_headers):
                _, next_pattern, _ = section_headers[i + 1]
                end_match = re.search(next_pattern, full_text[content_start:], re.IGNORECASE)
                content_end = content_start + end_match.start() if end_match else len(full_text)
            else:
                content_end = len(full_text)

            content = full_text[content_start:content_end].strip()
            content = re.sub(r"\*\*", "", content).strip()
            content = re.sub(label_pattern, "", content, flags=re.IGNORECASE).strip()
            content = content.lstrip(":").strip()
            if content:
                sections[key] = content

        if len(sections) >= 2:
            return FindingSections(
                contribucion=sections.get("contribucion", ""),
                metodologia=sections.get("metodologia", ""),
                resultados=sections.get("resultados", ""),
                implicaciones=sections.get("implicaciones", ""),
            )

        writeLog("warning", logger, "[FindingGenerator] Structured parsing failed; falling back to narrative")
        clean = re.sub(r"\*\*[^*]+:\*\*\s*", "", text).strip()
        return FindingSections(narrative=clean)

    def _clean_output(self, text: str) -> str:
        text = text.strip()
        for prefix in self._OUTPUT_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        return text

    def _parse_output(self, text: str, style: str) -> FindingSections:
        if style == "structured":
            return self._parse_structured_output(text)
        clean = self._clean_output(text)
        return FindingSections(narrative=clean)


# ---------------------------------------------------------------------------
# Capa 3 – Filtro de relevancia
# ---------------------------------------------------------------------------

class RelevanceFilter:
    def __init__(self, registry: ModelRegistry, config: SynthesisConfig) -> None:
        self._registry = registry
        self._cfg = config

    def filter(self, findings: List[RawFinding], concept_query: str,
               focus_terms: List[str]) -> List[RawFinding]:
        if not findings:
            return []
        candidates = [f for f in findings if f.avg_evidence_score >= self._cfg.min_finding_score]
        if not candidates:
            return []

        similarities = self._score_batch(candidates, concept_query, focus_terms)
        threshold = float(np.percentile(similarities, (1 - self._cfg.relevance_percentile) * 100))
        filtered = [f for f, sim in zip(candidates, similarities) if sim >= threshold]
        if not filtered:
            best_idx = int(np.argmax(similarities))
            filtered = [candidates[best_idx]]

        penalty = self._cfg.diversity_penalty
        if penalty > 0.0 and len(filtered) > 1:
            filtered = self._diversify(filtered, penalty)
        return filtered

    def _diversify(self, findings: List[RawFinding], penalty: float) -> List[RawFinding]:
        texts = [f.finding_text for f in findings]
        embs = self._registry.encode_batch(texts)
        sim_matrix = embs @ embs.T
        accepted_idx: List[int] = [0]
        for i in range(1, len(findings)):
            max_sim = max(sim_matrix[i, j] for j in accepted_idx)
            if max_sim <= (1.0 - penalty):
                accepted_idx.append(i)
        return [findings[i] for i in accepted_idx]

    def _score_batch(self, findings: List[RawFinding], concept_query: str,
                     focus_terms: List[str]) -> List[float]:
        concept_emb = self._registry.encode_batch([concept_query])[0]
        texts = [f.finding_text for f in findings]
        finding_embs = self._registry.encode_batch(texts)
        sims = (finding_embs @ concept_emb).tolist()
        if focus_terms:
            for i, finding in enumerate(findings):
                lower = finding.finding_text.lower()
                hits = sum(1 for t in focus_terms if t.lower() in lower)
                sims[i] += hits * self._cfg.focus_term_boost
        return sims


# ---------------------------------------------------------------------------
# Capa 4 – División de clusters
# ---------------------------------------------------------------------------

class ClusterSplitter:
    def __init__(self, registry: ModelRegistry, max_size: int) -> None:
        self._registry = registry
        self._max_size = max_size

    def split_if_needed(self, cluster: Dict) -> List[Dict]:
        if cluster["size"] <= self._max_size:
            return [cluster]
        evidences = cluster["evidences"]
        texts = [e["text"] for e in evidences]
        embeddings = self._registry.encode_batch(texts)

        import math
        n_sub = math.ceil(len(evidences) / self._max_size)

        from sklearn.cluster import AgglomerativeClustering
        clustering = AgglomerativeClustering(n_clusters=n_sub, metric="cosine", linkage="average")
        labels = clustering.fit_predict(embeddings)

        sub_clusters: Dict[int, List] = {}
        for ev, label in zip(evidences, labels):
            sub_clusters.setdefault(label, []).append(ev)

        result = []
        for sub_id, sub_evs in sub_clusters.items():
            if not sub_evs:
                continue
            scores = [e["similarity"] for e in sub_evs]
            doc_ids = list({e["doc_id"] for e in sub_evs})
            titles = list({e.get("paper_title", "") for e in sub_evs})
            result.append({
                "cluster_id": f"{cluster['cluster_id']}.{sub_id}",
                "size": len(sub_evs),
                "avg_evidence_score": float(np.mean(scores)),
                "focus_rich_count": cluster.get("focus_rich_count", 0),
                "focus_rich_percentage": cluster.get("focus_rich_percentage", 0),
                "suggested_theme": sub_evs[0]["text"][:150],
                "papers": doc_ids,
                "paper_titles": titles,
                "evidences": sub_evs,  # Ya enriquecidas
            })
        return result if result else [cluster]


# ---------------------------------------------------------------------------
# Capa 5 – Procesador de concepto
# ---------------------------------------------------------------------------

class ConceptSynthesizer:
    def __init__(
        self,
        generator: FindingGenerator,
        relevance_filter: RelevanceFilter,
        config: SynthesisConfig,
        splitter: Optional[ClusterSplitter] = None,
    ) -> None:
        self._generator = generator
        self._filter = relevance_filter
        self._cfg = config
        self._splitter = splitter

    def synthesize(
        self,
        concept_data: Dict,
        focus_terms: List[str],
    ) -> ConceptSynthesis:
        """
        Sintetiza hallazgos para un concepto.

        NOTA: concept_data debe venir de enriched_evidences.json
              (con structured_evidence ya en las evidencias)
        """
        concept_id = str(concept_data["concept_id"])
        concept_query = concept_data["concept_query"]
        clusters = concept_data.get("clusters", [])

        # Preparar clusters (posible división)
        prepared_clusters: List[Dict] = []
        for cluster in clusters:
            if (self._cfg.max_cluster_size_for_split is not None
                    and cluster["size"] > self._cfg.max_cluster_size_for_split
                    and self._splitter is not None):
                prepared_clusters.extend(self._splitter.split_if_needed(cluster))
            else:
                prepared_clusters.append(cluster)

        # Generar hallazgos
        raw_findings: List[RawFinding] = []
        for cluster in prepared_clusters:
            if cluster["size"] >= 2:
                finding = self._generator.generate(
                    concept_query, cluster, focus_terms
                )
                if finding is not None:
                    raw_findings.append(finding)
            elif (cluster["size"] == 1
                  and self._cfg.min_singleton_score is not None
                  and cluster["avg_evidence_score"] >= self._cfg.min_singleton_score):
                finding = self._generator.generate(
                    concept_query, cluster, focus_terms, force_style="brief"
                )
                if finding is not None:
                    raw_findings.append(finding)

        # Filtrar por relevancia
        filtered_findings = self._filter.filter(raw_findings, concept_query, focus_terms)

        # Agregar evidencias (ya vienen con structured_evidence)
        all_evidences = self._aggregate_evidences(clusters)

        # NUEVO: agregar resumen estructurado global del concepto
        aggregated_summary = self._generator._aggregate_concept_summary(tuple(filtered_findings))

        writeLog("info", logger,
                 f"[ConceptSynthesizer] Concept {concept_id}: "
                 f"{len(raw_findings)} raw → {len(filtered_findings)} filtered findings | "
                 f"{len(all_evidences)} evidences")

        return ConceptSynthesis(
            concept_id=concept_id,
            concept_query=concept_query,
            total_evidences=concept_data.get("total_evidences", 0),
            focus_terms_used=tuple(focus_terms),
            findings=tuple(filtered_findings),
            all_evidences=tuple(all_evidences),
            all_papers=tuple(set(e["doc_id"] for e in all_evidences)),
            aggregated_summary=aggregated_summary,
        )

    def _aggregate_evidences(self, clusters: List[Dict]) -> List[Dict]:
        """Agrega evidencias de todos los clusters (ya enriquecidas)."""
        evidences_by_key: Dict[Tuple, Dict] = {}

        for cluster in clusters:
            for ev in cluster.get("evidences", []):
                if ev.get("similarity", 0) < self._cfg.min_evidence_score:
                    continue
                key = (str(ev["doc_id"]), str(ev["text"]))
                if key not in evidences_by_key or ev["similarity"] > evidences_by_key[key]["similarity"]:
                    quote = {
                        "text": ev["text"],
                        "doc_id": ev["doc_id"],
                        "paper_title": ev.get("paper_title", "Unknown"),
                        "similarity": ev["similarity"],
                        "structured_evidence": ev.get("structured_evidence", {}),
                    }
                    evidences_by_key[key] = quote

        return sorted(evidences_by_key.values(), key=lambda e: e["similarity"], reverse=True)


# ---------------------------------------------------------------------------
# Capa 6 – Pipeline principal (LÓGICA PURA, sin I/O)
# ---------------------------------------------------------------------------

class SynthesisPipeline:
    """
    Pipeline de síntesis.

    RESPONSABILIDAD: Transformar datos enriquecidos en hallazgos sintetizados.
    NO hace I/O de archivos.
    """

    def __init__(self, synthesizer: ConceptSynthesizer, config: SynthesisConfig) -> None:
        self._synthesizer = synthesizer
        self._cfg = config

    def run(
        self,
        enriched_data: List[Dict],
        focus_terms: List[str],
    ) -> List[Dict]:
        """
        Ejecuta la síntesis sobre datos YA ENRIQUECIDOS.

        Args:
            enriched_data: Datos de enriched_evidences.json
            focus_terms: Términos de foco

        Returns:
            Lista de diccionarios con los hallazgos sintetizados
        """
        writeLog("info", logger,
                 f"[SynthesisPipeline] Generating findings for {len(enriched_data)} concepts...")

        syntheses = [
            self._synthesizer.synthesize(cd, focus_terms)
            for cd in enriched_data
        ]

        output = [s.to_dict() for s in syntheses]

        self._log_summary(syntheses)
        return output

    @staticmethod
    def _log_summary(syntheses: List[ConceptSynthesis]) -> None:
        sep = "=" * 60
        writeLog("info", logger, sep)
        writeLog("info", logger, "SYNTHESIS SUMMARY")
        writeLog("info", logger, sep)
        for s in syntheses:
            previews = [
                f['finding'][:100] + "..."
                for f in [f.to_dict() for f in s.findings]
                if f.get('finding')
            ]
            writeLog("info", logger,
                     f"Concept {s.concept_id}: {s.concept_query[:60]} | "
                     f"{len(s.findings)} findings | {len(s.all_evidences)} evidences")
            # Mostrar resumen estructurado
            agg = s.aggregated_summary
            if agg.get("signals") or agg.get("models"):
                writeLog("info", logger,
                         f"  Signals: {', '.join(agg.get('signals', [])[:5])}"
                         f"{' ...' if len(agg.get('signals', [])) > 5 else ''}")
                writeLog("info", logger,
                         f"  Models:  {', '.join(agg.get('models', [])[:5])}"
                         f"{' ...' if len(agg.get('models', [])) > 5 else ''}")
                if agg.get("metrics"):
                    metric_str = ", ".join(f"{k}: {v}" for k, v in agg.get("metrics", {}).items())[:100]
                    writeLog("info", logger, f"  Metrics: {metric_str}")
            for i, p in enumerate(previews):
                writeLog("info", logger, f"  Finding {i+1}: {p}")
        writeLog("info", logger, sep)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_pipeline(
    config: SynthesisConfig,
    llm_client: Optional[LLMClient] = None,
    registry: Optional[ModelRegistry] = None,
) -> SynthesisPipeline:
    """
    Construye el pipeline de síntesis.

    NOTA: Si no se proporciona un registry externo, se crea uno pero
          NO se carga automáticamente. El llamante debe asegurarse de
          llamar a registry.load() antes de usar el pipeline.
    """
    client = llm_client or create_resilient_ollama_client()

    if registry is None:
        registry = ModelRegistry(config.embedding_model_name)
        # Intencionalmente NO llamamos a load() aquí.
        # El llamante (processSynthesizeFindings) se encarga de cargarlo
        # y así puede controlar el momento exacto de la carga pesada.

    generator = FindingGenerator(client, config)
    relevance_filter = RelevanceFilter(registry, config)
    splitter = (
        ClusterSplitter(registry, config.max_cluster_size_for_split)
        if config.max_cluster_size_for_split is not None
        else None
    )
    synthesizer = ConceptSynthesizer(generator, relevance_filter, config, splitter)
    return SynthesisPipeline(synthesizer, config)


# ---------------------------------------------------------------------------
# Función de entrada (Capa de orquestación: I/O + lógica)
# ---------------------------------------------------------------------------

def processSynthesizeFindings() -> Optional[List[Dict]]:
    """
    Punto de entrada - Orquesta: leer → procesar → escribir.

    Lee: enriched_evidences.json, studyDescription.json
    Escribe: concept_findings.json
    """
    try:
        input_dir, output_dir = inicioModulo("processSynthesizeFindings")

        # --- LECTURA (I/O) ---
        enriched_file = output_dir / "enriched_evidences.json"
        if not enriched_file.exists():
            raise FileNotFoundError(
                f"enriched_evidences.json not found at {enriched_file}. "
                f"Run processEnrichEvidences() first."
            )
        enriched_data = read_json(enriched_file)

        config = SynthesisConfig.from_file(input_dir / "studyDescription.json")
        focus_data = read_json(input_dir / "studyDescription.json")
        focus_terms = focus_data.get("project", {}).get("focus_terms", [])

        # --- PROCESAMIENTO (lógica pura) ---
        registry = ModelRegistry(config.embedding_model_name)
        registry.load()

        pipeline = build_pipeline(config, registry=registry)
        findings = pipeline.run(
            enriched_data=enriched_data,
            focus_terms=focus_terms,
        )

        write_json(output_dir / "concept_findings.json", findings)
        return findings

    except Exception as e:
        writeLog("error", logger, f"Error in processSynthesizeFindings: {e}")
        raise RuntimeError(f"Error processSynthesizeFindings: {e}") from e


if __name__ == "__main__":
    processSynthesizeFindings()