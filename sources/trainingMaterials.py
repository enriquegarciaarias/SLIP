"""
trainingMaterials.py
====================
Genera el documento final de materiales de investigación (Markdown)
a partir de los datos producidos por el pipeline SLIP.

Arquitectura en capas (SOA) — separación MVC aplicada:
  ┌──────────────────────────────────────────────────────────────┐
  │             TrainingMaterialsPipeline  (orquestador)         │
  ├──────────────────────┬───────────────────────────────────────┤
  │  DocumentDataBuilder │  MarkdownRenderer                     │
  │  (agrega + filtra    │  (renderiza el modelo de datos        │
  │   datos del pipeline)│   a Markdown — sin lógica de negocio) │
  ├──────────────────────┴───────────────────────────────────────┤
  │  EmergingTopicAnalyzer   │  TranslationService (desde common)│
  │  (LLM → título+desc)     │  (caché disco + Google Translate) │
  ├──────────────────────────┴───────────────────────────────────┤
  │            LLMClient  (federado, suppress_thinking)          │
  └──────────────────────────────────────────────────────────────┘

Principios aplicados:
  - SRP  : DocumentDataBuilder construye datos; MarkdownRenderer los presenta.
  - OCP  : MarkdownRenderer sustituible por HtmlRenderer, PdfRenderer, etc.
           sin tocar DocumentDataBuilder ni el pipeline.
  - DIP  : LLMClient inyectado, no instanciado aquí.
  - Sin globals mutables.
  - Traducción externalizada a common.translationService.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from tqdm import tqdm

from sources.common.common import logger, writeLog
from sources.common.llm_client import LLMClient, create_ollama_client
from sources.common.referenceFilter import looks_like_reference
from sources.common.translationService import TranslationService
from sources.common.utils import inicioModulo
from sources.manuscriptEvidence import (
    ManuscriptEvidence,
    ManuscriptEvidenceBuilder,
    build_manuscript_refs,
    emerging_enabled,
    manuscript_enabled,
)
from sources.enrich_evidences import EnrichmentConfig, PaperSectionExtractor


# ---------------------------------------------------------------------------
# Configuración tipada
# ---------------------------------------------------------------------------

@dataclass
class TrainingConfig:
    llm_model: str = "qwen3:8b"
    off_topic_threshold: float = 0.30
    min_display_similarity: float = 0.35
    max_evidences_to_show: int = 30
    max_chars_per_emerging: int = 8000
    translate_evidences: bool = False
    translation_cache_file: str = "translation_cache.json"
    translation_max_chars: int = 3000
    translation_backend: str = "marian"
    translation_model: str = "Helsinki-NLP/opus-mt-en-es"
    filter_reference_fragments: bool = True
    llm_temperature: float = 0.2
    llm_max_tokens: int = 1024
    emerging_max_tokens: int = 2048
    min_finding_score: float = 0.40
    # Flags de ejecución (--emergente / --manuscript); se fijan en el entry point.
    emerging_enabled: bool = True
    manuscript_enabled: bool = False
    manuscript_extension_max_tokens: int = 300

    @classmethod
    def from_project_config(cls, project: Dict) -> "TrainingConfig":
        t = project.get("training_materials", {})
        return cls(
            llm_model=t.get("llm_model", "qwen3:8b"),
            off_topic_threshold=t.get("off_topic_threshold", 0.30),
            min_display_similarity=t.get("min_display_similarity", 0.35),
            max_evidences_to_show=t.get("max_evidences_to_show", 30),
            max_chars_per_emerging=t.get("max_chars_per_emerging", 8000),
            translate_evidences=t.get("translate_evidences", False),
            min_finding_score=t.get("min_finding_score", 0.40),
            translation_backend=t.get("translation_backend", "marian"),
            translation_model=t.get(
                "translation_model", "Helsinki-NLP/opus-mt-en-es"
            ),
            filter_reference_fragments=t.get("filter_reference_fragments", True),
        )

    @classmethod
    def from_file(cls, study_file: Path) -> "TrainingConfig":
        if not study_file.exists():
            return cls()
        with open(study_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_project_config(data.get("project", {}))


# ---------------------------------------------------------------------------
# Modelo de datos del documento (capa M de MVC)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceItem:
    doc_id: str
    paper_title: str
    text: str
    similarity: float
    structured_evidence: Dict = field(default_factory=dict, compare=False, hash=False)
    narrative_summary: str = ""


@dataclass(frozen=True)
class FindingItem:
    finding_text: str
    evidence_count: int
    avg_evidence_score: float
    top_quotes: Tuple[EvidenceItem, ...]
    citations: Dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ConceptCard:
    concept_id: str
    concept_query: str
    concept_type: Optional[str]
    focus_density: Optional[float]
    findings: Tuple[FindingItem, ...]
    all_evidences: Tuple[EvidenceItem, ...]
    represented_papers: Tuple[str, ...]
    represented_titles: Dict[str, str]


@dataclass(frozen=True)
class EmergingTopic:
    concept_id: str
    title: str
    description: str
    keywords: Tuple[str, ...]
    paper_ids: Tuple[str, ...]


@dataclass(frozen=True)
class SearchStrategyStats:
    query_text: str
    objectives_text: str   # NUEVO: objetivos de investigación
    n_objective_concepts: int
    n_candidates: int
    n_aligned: int
    n_emergent: int
    relevance_threshold: float
    min_docs_after_rerank: str
    focus_terms_used: bool


@dataclass(frozen=True)
class PrismaStats:
    """Números del flujo PRISMA derivados directamente del pipeline."""
    db_counts: Dict[str, int]
    total_identified: int
    duplicates_removed: int
    after_dedup: int
    records_screened: int
    excluded_screening: int
    sought_for_retrieval: int
    not_retrieved: int
    assessed_for_eligibility: int
    included_in_concepts: int
    excluded_not_in_concepts: int
    excluded_emerging_only: int
    excluded_no_concept: int
    not_retrieved_papers: Tuple[Tuple[str, str], ...]      # (paper_id, title)
    excluded_emerging_papers: Tuple[Tuple[str, str], ...]  # (paper_id, title)
    excluded_no_concept_papers: Tuple[Tuple[str, str], ...]  # (paper_id, title)


class CitationRegistry:
    """Asigna un número de referencia global, por orden de primera aparición."""

    def __init__(self) -> None:
        self._numbers: Dict[str, int] = {}

    def number(self, doc_id: str) -> int:
        if doc_id not in self._numbers:
            self._numbers[doc_id] = len(self._numbers) + 1
        return self._numbers[doc_id]

    def is_empty(self) -> bool:
        return not self._numbers

    def ordered(self) -> Tuple[Tuple[str, int], ...]:
        return tuple(sorted(self._numbers.items(), key=lambda kv: kv[1]))


@dataclass(frozen=True)
class DocumentModel:
    generated_at: str
    search_stats: SearchStrategyStats
    prisma: PrismaStats
    concept_cards: Tuple[ConceptCard, ...]
    emerging_topics: Tuple[EmergingTopic, ...]
    technical_concepts: Tuple[Dict, ...]
    bibliography: Dict[str, Dict]  # doc_id -> {title, doi, citation_count, bibtex, group}
    manuscript_evidence: Optional["ManuscriptEvidence"] = None


# ---------------------------------------------------------------------------
# Capa 1 – Analizador de tópicos emergentes
# ---------------------------------------------------------------------------

def build_bibtex(paper_id: str, meta: Dict) -> str:
    """
    Genera una entrada BibTeX a partir de la metadata del pipeline.
    paper_id se usa como clave de la entrada.
    """
    title = (meta.get("title") or paper_id).replace("{", "").replace("}", "")
    authors = meta.get("authors") or []
    year = meta.get("year")
    venue = (meta.get("venue") or "").replace("{", "").replace("}", "")
    doi = meta.get("doi") or ""

    is_proceedings = bool(re.search(r"conference|symposium|workshop", venue, re.IGNORECASE))
    entry_type = "@inproceedings" if is_proceedings else "@article"

    lines = [f"{entry_type}{{{paper_id},"]
    if authors:
        lines.append(f"  author = {{{' and '.join(str(a) for a in authors)}}},")
    lines.append(f"  title = {{{title}}},")
    if year:
        lines.append(f"  year = {{{year}}},")
    if venue:
        key = "booktitle" if is_proceedings else "journal"
        lines.append(f"  {key} = {{{venue}}},")
    if doi:
        lines.append(f"  doi = {{{doi}}},")
    lines.append("}")
    return "\n".join(lines)


class EmergingTopicAnalyzer:
    """
    Genera título y descripción de un tópico emergente en una única
    llamada LLM con output JSON. Responsabilidad única: prompt → EmergingTopic.
    """

    _SYSTEM_PROMPT = (
        "You are a research assistant. Analyze the provided academic paper fragments. "
        "Return ONLY a JSON object with 'title' (max 10 words, Spanish) and "
        "'description' (200-300 words, Spanish). No markdown fences."
    )

    def __init__(self, llm_client: LLMClient, config: TrainingConfig) -> None:
        self._llm = llm_client
        self._cfg = config

    def analyze(self, concept: Dict, papers_text: Dict) -> EmergingTopic:
        concept_id = str(concept.get("concept_id", "unknown"))
        fallback_title = concept.get("label", "Concepto emergente")
        paper_ids = concept.get("document_indices", [])
        keywords = tuple(concept.get("keywords", [])[:8])

        title, description = self._generate(paper_ids, papers_text, fallback_title)
        return EmergingTopic(
            concept_id=concept_id,
            title=title,
            description=description,
            keywords=keywords,
            paper_ids=tuple(paper_ids),
        )

    def _generate(
        self, paper_ids: List[str], papers_text: Dict, fallback_title: str
    ) -> Tuple[str, str]:
        if not paper_ids:
            return fallback_title, "No hay papers asociados."

        combined = self._build_text(paper_ids, papers_text)
        if not combined.strip():
            return fallback_title, "No hay texto suficiente para analizar."

        raw = self._llm.generate_json(
            prompt=(
                f"Academic paper fragments from a discovered topic:\n\n{combined}\n\n"
                'Return JSON: {"title": "...", "description": "..."}'
            ),
            system_prompt=self._SYSTEM_PROMPT,
            temperature=self._cfg.llm_temperature,
            max_tokens=self._cfg.emerging_max_tokens,
            context=f"emerging:{fallback_title[:30]}",
            expect_array=False,
        )

        title = re.sub(r"[*#]", "", raw.get("title", "")).strip()[:100] or fallback_title
        description = re.sub(r"\s+", " ", raw.get("description", "")).strip()
        return title, description or "No se pudo generar una descripción automática."

    def _build_text(self, paper_ids: List[str], papers_text: Dict) -> str:
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
            full = " ".join(p for p in parts if p)[:1500]
            if full:
                texts.append(full)
        return "\n\n".join(texts)[: self._cfg.max_chars_per_emerging]


# ---------------------------------------------------------------------------
# Capa 2 – Constructor del modelo de datos (lógica de negocio pura)
# ---------------------------------------------------------------------------

class DocumentDataBuilder:
    def __init__(
        self,
        config: TrainingConfig,
        emerging_analyzer: EmergingTopicAnalyzer,
    ) -> None:
        self._cfg = config
        self._analyzer = emerging_analyzer

    def build(
        self,
        findings_data: List[Dict],
        concepts_query: Dict,
        aligned_concepts: Dict,
        candidate_concepts: Dict,
        concepts_with_tech: List[Dict],
        papers_text: Dict,
        objectives_text: str,                     # NUEVO
        metadata_lookup: Dict[str, Dict],         # NUEVO
        prisma_raw: Dict,                         # NUEVO: conteos brutos para PRISMA
        manuscript_evidence: Optional[ManuscriptEvidence] = None,
    ) -> DocumentModel:
        aligned_lookup = {
            str(ac.get("concept_id")): ac
            for ac in aligned_concepts.get("aligned_concepts", [])
        }
        search_stats, emergent_ids = self._build_search_stats(
            concepts_query, aligned_concepts, candidate_concepts, objectives_text
        )

        concept_cards = []
        for c_data in tqdm(findings_data, desc="Construyendo tarjetas de concepto"):
            concept_cards.append(self._build_concept_card(c_data, aligned_lookup))
        concept_cards = tuple(concept_cards)

        included_ids = set()
        for fd in findings_data:
            included_ids.update(fd.get("all_papers", []))

        if self._cfg.emerging_enabled:
            emerging_topics = self._build_emerging_topics(
                emergent_ids, candidate_concepts, papers_text
            )
            emerging_paper_ids = set()
            for topic in emerging_topics:
                emerging_paper_ids.update(topic.paper_ids)
        else:
            # No se desarrollan los tópicos emergentes (ni se llama al LLM),
            # pero se conservan sus papers para PRISMA y bibliografía.
            emerging_topics = ()
            emerging_paper_ids = set()
            id_set = set(emergent_ids)
            for concept in candidate_concepts.get("concepts", []):
                if str(concept.get("concept_id")) in id_set:
                    emerging_paper_ids.update(concept.get("document_indices", []))
            writeLog("info", logger,
                     "[Builder] Conceptos emergentes desactivados (--emergente 0): "
                     "no se generan títulos/descripciones.")

        bibliography = self._build_bibliography(
            included_ids, emerging_paper_ids, metadata_lookup
        )
        prisma = self._build_prisma(
            papers_text, candidate_concepts, included_ids, emerging_paper_ids,
            prisma_raw, metadata_lookup,
        )

        return DocumentModel(
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            search_stats=search_stats,
            prisma=prisma,
            concept_cards=concept_cards,
            emerging_topics=emerging_topics,
            technical_concepts=tuple(concepts_with_tech),
            bibliography=bibliography,
            manuscript_evidence=manuscript_evidence,
        )

    def _build_search_stats(
        self,
        concepts_query: Dict,
        aligned_concepts: Dict,
        candidate_concepts: Dict,
        objectives_text: str,
    ) -> Tuple[SearchStrategyStats, List[str]]:
        aligned_ids = {
            str(ac.get("concept_id"))
            for ac in aligned_concepts.get("aligned_concepts", [])
        }
        candidate_list = candidate_concepts.get("concepts", [])
        emergent_ids = [
            str(c.get("concept_id"))
            for c in candidate_list
            if str(c.get("concept_id")) not in aligned_ids
        ]
        stats = SearchStrategyStats(
            query_text=re.sub(r"\s+", " ", concepts_query.get("query", "")).strip(),
            objectives_text=objectives_text,
            n_objective_concepts=len(concepts_query.get("concepts", [])),
            n_candidates=candidate_concepts.get("n_topics", 0),
            n_aligned=len(aligned_concepts.get("aligned_concepts", [])),
            n_emergent=len(emergent_ids),
            relevance_threshold=aligned_concepts.get(
                "alignment_threshold", self._cfg.off_topic_threshold
            ),
            min_docs_after_rerank=str(
                aligned_concepts.get("min_docs_after_rerank", "No especificado")
            ),
            focus_terms_used=bool(aligned_concepts.get("focus_terms_used", False)),
        )
        return stats, emergent_ids

    def _build_concept_card(
        self, concept_data: Dict, aligned_lookup: Dict
    ) -> ConceptCard:
        concept_id = str(concept_data["concept_id"])
        threshold = self._cfg.min_display_similarity
        min_score = self._cfg.min_finding_score
        aligned = aligned_lookup.get(concept_id, {})

        def make_evidence(ev: Dict) -> EvidenceItem:
            return EvidenceItem(
                doc_id=ev["doc_id"],
                paper_title=ev.get("paper_title", ""),
                text=re.sub(r"\s+", " ", ev.get("text", "")).strip(),
                similarity=ev["similarity"],
                structured_evidence=ev.get("structured_evidence", {}),
                narrative_summary=ev.get("narrative_summary", ""),
            )

        valid_evidences = tuple(
            make_evidence(ev)
            for ev in concept_data.get("all_evidences", [])
            if ev.get("similarity", 0) >= threshold
        )

        findings: List[FindingItem] = []
        for f in concept_data.get("findings", []):
            if f.get("avg_evidence_score", 0.0) < min_score:
                writeLog(
                    "debug",
                    logger,
                    f"[Builder] Finding dropped (low score: {f.get('avg_evidence_score', 0):.2f} < {min_score})",
                )
                continue
            quotes = tuple(
                make_evidence(q)
                for q in f.get("top_quotes", [])
                if q.get("similarity", 0) >= threshold
            )
            citations = {
                int(c["ref"]): c["doc_id"]
                for c in f.get("citations", [])
                if isinstance(c, dict) and "ref" in c and "doc_id" in c
            }
            if quotes:
                findings.append(
                    FindingItem(
                        finding_text=f.get("finding", ""),
                        evidence_count=f.get("evidence_count", 0),
                        avg_evidence_score=f.get("avg_evidence_score", 0.0),
                        top_quotes=quotes,
                        citations=citations,
                    )
                )

        # --- Limpieza determinista de fragmentos de bibliografía ---
        before = len(valid_evidences)

        def _keep(ev: EvidenceItem) -> bool:
            return not (
                self._cfg.filter_reference_fragments and looks_like_reference(ev.text)
            )

        if self._cfg.filter_reference_fragments:
            valid_evidences = tuple(ev for ev in valid_evidences if _keep(ev))
            filtered_findings: List[FindingItem] = []
            for fi in findings:
                quotes = tuple(q for q in fi.top_quotes if _keep(q))
                if not quotes:
                    writeLog(
                        "debug",
                        logger,
                        f"[Builder] Finding dropped (sin evidencias válidas) "
                        f"concept={concept_id}",
                    )
                    continue
                filtered_findings.append(
                    replace(fi, top_quotes=quotes)
                    if len(quotes) != len(fi.top_quotes) else fi
                )
            findings = filtered_findings
            dropped = before - len(valid_evidences)
            if dropped:
                writeLog(
                    "info",
                    logger,
                    f"[Builder] Concepto {concept_id}: descartadas "
                    f"{dropped} evidencias no válidas",
                )

        paper_ids: Dict[str, str] = {ev.doc_id: ev.paper_title for ev in valid_evidences}
        for fi in findings:
            for q in fi.top_quotes:
                paper_ids[q.doc_id] = q.paper_title

        concept_type = aligned.get("concept_type")
        return ConceptCard(
            concept_id=concept_id,
            concept_query=concept_data.get("concept_query", ""),
            concept_type=concept_type if concept_type and concept_type != "default" else None,
            focus_density=aligned.get("focus_density"),
            findings=tuple(findings),
            all_evidences=valid_evidences,
            represented_papers=tuple(paper_ids.keys()),
            represented_titles=paper_ids,
        )

    def _build_emerging_topics(
        self, emergent_ids: List[str], candidate_concepts: Dict, papers_text: Dict
    ) -> Tuple[EmergingTopic, ...]:
        id_set = set(emergent_ids)
        emergent_concepts = [
            c for c in candidate_concepts.get("concepts", [])
            if str(c.get("concept_id")) in id_set
        ]
        topics = []
        for concept in tqdm(emergent_concepts, desc="Generando tópicos emergentes"):
            topics.append(self._analyzer.analyze(concept, papers_text))
        return tuple(topics)

    @staticmethod
    def _build_bibliography(
        included_ids: set,
        emerging_paper_ids: set,
        metadata_lookup: Dict[str, Dict],
    ) -> Dict[str, Dict]:
        """
        Construye la bibliografía con BibTeX, diferenciando:
          - group="concept"  : papers que entran en los conceptos (incluidos).
          - group="emerging" : papers que solo aparecen en conceptos emergentes.
        """
        bibliography: Dict[str, Dict] = {}
        for doc_id in sorted(included_ids):
            meta = metadata_lookup.get(doc_id, {})
            bibliography[doc_id] = {
                "title": meta.get("title", doc_id),
                "doi": meta.get("doi", ""),
                "citation_count": meta.get("citation_count", 0),
                "bibtex": build_bibtex(doc_id, meta),
                "group": "concept",
            }
        for doc_id in sorted(emerging_paper_ids):
            if doc_id in bibliography:
                continue
            meta = metadata_lookup.get(doc_id, {})
            bibliography[doc_id] = {
                "title": meta.get("title", doc_id),
                "doi": meta.get("doi", ""),
                "citation_count": meta.get("citation_count", 0),
                "bibtex": build_bibtex(doc_id, meta),
                "group": "emerging",
            }
        return bibliography

    @staticmethod
    def _build_prisma(
        papers_text: Dict,
        candidate_concepts: Dict,
        included_ids: set,
        emerging_paper_ids: set,
        prisma_raw: Dict,
        metadata_lookup: Dict[str, Dict],
    ) -> PrismaStats:
        """
        Deriva los números del flujo PRISMA a partir de los datos del pipeline.
        prisma_raw: {"db_counts": {..}, "n_duplicates": int, "n_sought": int,
                     "n_not_retrieved": int}
        """
        db_counts = prisma_raw.get("db_counts", {})
        total_identified = sum(db_counts.values())
        duplicates_removed = prisma_raw.get("n_duplicates", 0)
        after_dedup = total_identified - duplicates_removed
        records_screened = after_dedup
        sought = prisma_raw.get("n_sought", 0)
        excluded_screening = max(0, records_screened - sought)
        not_retrieved = prisma_raw.get("n_not_retrieved", 0)
        assessed = len(papers_text)
        included = len(included_ids)
        emerging_only = sorted(emerging_paper_ids - included_ids)
        full_text_ids = set(papers_text.keys())
        no_concept = sorted(full_text_ids - included_ids - emerging_paper_ids)

        def _title(pid: str) -> str:
            meta_title = metadata_lookup.get(pid, {}).get("title", "")
            if meta_title:
                return meta_title
            paper = papers_text.get(pid)
            return (paper or {}).get("title", "") or pid

        not_retrieved_papers = [
            (str(x.get("paper_id")), x.get("title") or "")
            for x in prisma_raw.get("not_retrieved_papers", [])
        ]

        return PrismaStats(
            db_counts=dict(db_counts),
            total_identified=total_identified,
            duplicates_removed=duplicates_removed,
            after_dedup=after_dedup,
            records_screened=records_screened,
            excluded_screening=excluded_screening,
            sought_for_retrieval=sought,
            not_retrieved=not_retrieved,
            assessed_for_eligibility=assessed,
            included_in_concepts=included,
            excluded_not_in_concepts=len(emerging_only) + len(no_concept),
            excluded_emerging_only=len(emerging_only),
            excluded_no_concept=len(no_concept),
            not_retrieved_papers=tuple(not_retrieved_papers),
            excluded_emerging_papers=tuple((pid, _title(pid)) for pid in emerging_only),
            excluded_no_concept_papers=tuple((pid, _title(pid)) for pid in no_concept),
        )


# ---------------------------------------------------------------------------
# Capa 3 – Renderer Markdown (presentación pura)
# ---------------------------------------------------------------------------

class MarkdownRenderer:
    """
    Convierte DocumentModel → string Markdown.
    No filtra datos, no llama al LLM, no lee disco.
    """

    _CITE_RE = re.compile(r"\[(\d+)\]")

    _STRENGTH_MAP = (
        (5, 0.70, "Muy Alta", "🔴"),
        (3, 0.65, "Alta",     "🟠"),
        (2, 0.60, "Media",    "🟡"),
        (1, 0.00, "Baja",     "🟢"),
    )

    def __init__(self, config: TrainingConfig, translation_svc: TranslationService) -> None:
        self._cfg = config
        self._translation_svc = translation_svc

    def render(self, model: DocumentModel) -> str:
        # Registro global de citas, numeradas por orden de primera aparición.
        self._citations = CitationRegistry()
        for card in model.concept_cards:
            for finding in card.findings:
                for ref in sorted(finding.citations):
                    self._citations.number(finding.citations[ref])

        sections = [
            self._render_header(model),
            self._render_search_stats(model.search_stats),
            "---\n",
            self._render_prisma(model.prisma),
            "---\n",
            self._render_concept_summary(model.concept_cards),
            "---\n",
            self._render_concept_cards(model.concept_cards),
        ]
        if self._cfg.emerging_enabled:
            sections.append(self._render_emerging_topics(model.emerging_topics))
        if model.manuscript_evidence is not None:
            sections.append(self._render_manuscript_evidence(model.manuscript_evidence))
        if not self._citations.is_empty():
            sections.append("---\n")
            sections.append(self._render_cited_references(model.bibliography))
        sections.extend([
            "---\n",
            self._render_technical_annex(model.technical_concepts),
            "---\n",
            self._render_bibliography(model.bibliography),
            self._render_footer(),
        ])
        return "\n".join(sections)

    def _translate_with_citations(
        self, text: str, citations: Dict[int, str]
    ) -> str:
        """
        Traduce el texto preservando los marcadores de cita y remapeando las
        referencias locales (índice de evidencia) a la numeración global.
        Los marcadores fuera de rango se descartan.
        """
        if not text:
            return text
        # Sin mapa de citas (p. ej. salidas antiguas) se traduce tal cual,
        # preservando cualquier corchete del texto original.
        if not citations:
            return self._translation_svc.translate(text)
        parts = self._CITE_RE.split(text)
        out: List[str] = []
        for i, part in enumerate(parts):
            if i % 2 == 1:
                doc_id = citations.get(int(part))
                if doc_id:
                    out.append(f"[{self._citations.number(doc_id)}]")
            else:
                out.append(self._translation_svc.translate(part) if part.strip() else part)
        return "".join(out)

    def _render_cited_references(self, bibliography: Dict[str, Dict]) -> str:
        """Lista numerada de las fuentes citadas inline (trazabilidad)."""
        lines = ["## Referencias citadas (atribución por afirmación)\n"]
        for doc_id, number in self._citations.ordered():
            info = bibliography.get(doc_id, {})
            title = info.get("title") or doc_id
            bits = [f"**[{number}]** {title}"]
            if info.get("doi"):
                bits.append(f"DOI: `{info['doi']}`")
            bits.append(f"ID: `{doc_id}`")
            lines.append("- " + " — ".join(bits))
        return "\n".join(lines) + "\n"

    def _render_header(self, model: DocumentModel) -> str:
        return (
            "# Scientific Literature Intelligence Pipeline (SLIP)\n\n"
            f"**Generado:** {model.generated_at}\n\n---\n"
        )

    def _render_search_stats(self, stats: SearchStrategyStats) -> str:
        # Reemplazamos "Query utilizada" por "Objetivos de la investigación"
        return (
            "## Evaluación de la estrategia de búsqueda\n\n"
            f"### Objetivos de la investigación\n{stats.objectives_text}\n\n"
            "### Estadísticas\n"
            f"- **Conceptos objetivo:** {stats.n_objective_concepts}\n"
            f"- **Tópicos descubiertos:** {stats.n_candidates}\n"
            f"- **Tópicos alineados:** {stats.n_aligned}\n"
            f"- **Tópicos emergentes:** {stats.n_emergent}\n"
            f"- **Umbral de relevancia:** {stats.relevance_threshold}\n"
            f"- **Mín. docs por concepto:** {stats.min_docs_after_rerank}\n"
            f"- **Focus terms:** {'Sí' if stats.focus_terms_used else 'No'}\n"
        )

    def _render_prisma(self, stats: PrismaStats) -> str:
        db = ", ".join(f"{k}: {v}" for k, v in stats.db_counts.items())
        return (
            "## Flujo PRISMA (derivado del pipeline)\n\n"
            "### Identificación\n"
            f"- **Registros identificados en bases de datos:** {stats.total_identified} ({db})\n"
            f"- **Duplicados eliminados:** {stats.duplicates_removed}\n"
            f"- **Registros tras eliminación de duplicados:** {stats.after_dedup}\n\n"
            "### Cribado y elegibilidad\n"
            f"- **Registros cribados:** {stats.records_screened}\n"
            f"- **Registros excluidos en el cribado:** {stats.excluded_screening}\n"
            f"- **Publicaciones buscadas para su recuperación:** {stats.sought_for_retrieval}\n"
            f"- **Publicaciones no recuperadas (restricciones de acceso):** {stats.not_retrieved}\n"
            f"- **Publicaciones evaluadas para elegibilidad:** {stats.assessed_for_eligibility}\n\n"
            "### Inclusión\n"
            f"- **Estudios incluidos en conceptos:** {stats.included_in_concepts}\n"
            f"- **Excluidos (no entran en conceptos):** {stats.excluded_not_in_concepts} "
            f"(solo emergentes: {stats.excluded_emerging_only}, sin concepto: {stats.excluded_no_concept})\n\n"
            "> Nota: los motivos de exclusión identificables automáticamente son duplicados entre "
            "bases de datos y no recuperados por restricciones de acceso. El resto de motivos se "
            "agrupará a posteriori.\n\n"
            + self._render_prisma_annex(stats)
        )

    @staticmethod
    def _render_prisma_annex(stats: PrismaStats) -> str:
        """Anexo de apoyo al diagrama PRISMA: lista los papers excluidos y no recuperados."""
        def _list(items: Tuple[Tuple[str, str], ...]) -> str:
            if not items:
                return "Ninguno.\n"
            lines = []
            for pid, title in sorted(items):
                short = re.sub(r"\s+", " ", title)[:90]
                lines.append(f"- `{pid}` — {short}")
            return "\n".join(lines)

        return (
            "#### Anexo: papers no recuperados y excluidos\n\n"
            f"**No recuperados — restricciones de acceso ({len(stats.not_retrieved_papers)}):**\n"
            f"{_list(stats.not_retrieved_papers)}\n\n"
            f"**Solo en conceptos emergentes ({len(stats.excluded_emerging_papers)}):**\n"
            f"{_list(stats.excluded_emerging_papers)}\n\n"
            f"**Sin concepto ni emergente ({len(stats.excluded_no_concept_papers)}):**\n"
            f"{_list(stats.excluded_no_concept_papers)}\n"
        )

    def _render_concept_summary(self, cards: Tuple[ConceptCard, ...]) -> str:
        lines = ["## Resumen de hallazgos por concepto objetivo\n"]

        for card in cards:
            lines.append(f"**Concepto {card.concept_id}:** {card.concept_query}\n")

            if not card.findings:
                lines.append("*Sin hallazgos sintetizados*\n")
                continue

            for idx, finding in enumerate(card.findings, 1):
                finding_text = self._translate_with_citations(
                    finding.finding_text, finding.citations
                )
                lines.append(f"**Hallazgo {idx}:**")
                lines.append(f"> {finding_text}\n")

                if finding.citations:
                    refs = "".join(
                        f"[{self._citations.number(finding.citations[r])}]"
                        for r in sorted(finding.citations)
                    )
                    lines.append(f"**Fuentes:** {refs}\n")
                else:
                    paper_ids = sorted(set(q.doc_id for q in finding.top_quotes))
                    if paper_ids:
                        lines.append(f"**Papers:** {', '.join(paper_ids)}\n")
                    else:
                        lines.append("**Papers:** Sin papers asociados directamente\n")

        return "\n".join(lines)

    def _render_concept_cards(self, cards: Tuple[ConceptCard, ...]) -> str:
        return "\n---\n".join(self._render_concept_card(c) for c in cards) + "\n---\n"

    def _render_concept_card(self, card: ConceptCard) -> str:
        header = f"## Concepto {card.concept_id}: {card.concept_query}\n"

        meta = []
        if card.concept_type:
            meta.append(f"Tipo: `{card.concept_type}`")
        if card.focus_density is not None:
            meta.append(f"Densidad de foco: {card.focus_density * 100:.0f}%")
        if meta:
            header += f"*{' · '.join(meta)}*\n"

        # MODIFICADO: solo mostramos el número de papers, sin los IDs
        if card.represented_papers:
            header += f"\n**Papers representados:** {len(card.represented_papers)}\n"

        if not card.findings:
            if card.all_evidences:
                return (
                    header
                    + "\n### Estado: SIN HALLAZGOS SINTETIZADOS\n\n"
                    + "Se encontraron fragmentos relevantes sin hallazgos consolidados.\n\n"
                    + "**Evidencias textuales:**\n\n"
                    + self._render_evidence_blocks(card.all_evidences)
                )
            return header + "\n### Estado: SIN EVIDENCIA SUFICIENTE\n"

        parts = [header]
        for idx, finding in enumerate(card.findings, 1):
            strength, emoji = self._evidence_strength(
                finding.avg_evidence_score, finding.evidence_count
            )
            finding_text = self._translate_with_citations(
                finding.finding_text, finding.citations
            )
            parts.append(
                f"\n### Hallazgo {idx}: {emoji} {strength}\n\n"
                f"> {finding_text}\n\n"
                "**Evidencias textuales:**\n\n"
                + self._render_evidence_blocks(finding.top_quotes)
            )
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Renderizado de evidencias preservando la estructura enriquecida
    # ------------------------------------------------------------------

    def _render_evidence_blocks(self, evidences: Tuple[EvidenceItem, ...]) -> str:
        shown = evidences[: self._cfg.max_evidences_to_show]
        if not shown:
            return f"No hay evidencias con similitud ≥ {self._cfg.min_display_similarity}.\n"

        # El structured_evidence (y el resumen narrativo, cuando no hay datos
        # estructurados) es a nivel de paper: se muestra solo la primera vez que
        # aparece cada paper en el bloque, para no repetir la misma información
        # en cada fragmento textual sin descartar ningún campo.
        rendered_structured: set[str] = set()
        blocks = []
        for ev in shown:
            # El extracto verba se conserva en su idioma original (fidelidad);
            # solo los textos extensos de "Datos estructurados" se traducen.
            title = ev.paper_title if ev.paper_title and ev.paper_title != "Unknown" else f"ID: {ev.doc_id}"

            block = (
                f"- **{title}** "
                f"(sim: {ev.similarity:.3f}) (ID: `{ev.doc_id}`)\n"
                f'  *"{ev.text}"*\n'
            )

            if ev.doc_id in rendered_structured:
                block += "\n  *(Datos estructurados del paper ya mostrados arriba)*\n"
                blocks.append(block)
                continue

            structured_md = self._render_structured_evidence(ev.structured_evidence)
            if structured_md:
                rendered_structured.add(ev.doc_id)
                block += f"\n{structured_md}\n"
            elif ev.narrative_summary:
                # Solo si no hay datos estructurados se recurre al resumen
                # narrativo, que es una síntesis derivada de ellos (lossy).
                rendered_structured.add(ev.doc_id)
                translated_summary = self._translation_svc.translate(ev.narrative_summary)
                block += f"\n  *Resumen narrativo:* {translated_summary}\n"

            blocks.append(block)

        return "\n".join(blocks)

    # Orden y etiquetas de los campos de structured_evidence.
    _STRUCTURED_FIELDS: Tuple[Tuple[str, str], ...] = (
        ("ideas", "Ideas / contexto"),
        ("solution", "Contribución principal"),
        ("usage", "Contexto de uso"),
        ("methods", "Metodología"),
        ("results", "Resultados"),
        ("applications", "Aplicaciones"),
        ("gap", "Brecha abordada"),
        ("evolutions", "Trabajo futuro"),
        ("signals", "Señales"),
        ("models", "Modelos"),
        ("metrics", "Métricas"),
        ("limitations", "Limitaciones"),
    )

    def _render_structured_evidence(self, structured: Dict) -> str:
        """
        Renderiza `structured_evidence` conservando su estructura: un campo
        etiquetado por línea, sin colapsarlos en un único párrafo. Traduce cada
        valor y preserva listas (elemento a elemento) y diccionarios (clave:
        valor) completos. Cualquier clave nueva no contemplada en el orden
        conocido se añade igualmente para no perder información.
        """
        if not structured:
            return ""

        indent = "  "
        lines: List[str] = [f"{indent}**Datos estructurados:**"]

        def _join(seq) -> str:
            return "; ".join(
                self._translation_svc.translate(str(v)) for v in seq if str(v).strip()
            )

        def _render_value(val) -> str:
            if isinstance(val, dict):
                return "; ".join(
                    f"{self._translation_svc.translate(str(k))}: "
                    f"{self._translation_svc.translate(str(v))}"
                    for k, v in val.items() if k and v
                )
            if isinstance(val, (list, tuple, set)):
                return _join(val)
            return self._translation_svc.translate(str(val))

        known = {key for key, _ in self._STRUCTURED_FIELDS}
        for key, label in self._STRUCTURED_FIELDS:
            val = structured.get(key)
            if not val:
                continue
            rendered = _render_value(val)
            if rendered:
                lines.append(f"{indent}- **{label}:** {rendered}")

        for key, val in structured.items():
            if key in known or not val:
                continue
            rendered = _render_value(val)
            if rendered:
                label = key.replace("_", " ").capitalize()
                lines.append(f"{indent}- **{label}:** {rendered}")

        return "\n".join(lines) if len(lines) > 1 else ""

    # ------------------------------------------------------------------
    # Métodos legados (mantenidos por compatibilidad)
    # ------------------------------------------------------------------

    def _render_structured(self, structured: Dict) -> str:
        if not structured:
            return ""
        parts = []
        for key, label in (
            ("ideas", "Ideas principales"),
            ("metodos", "Métodos"),
            ("resultados", "Resultados"),
            ("aplicaciones", "Aplicaciones"),
        ):
            val = structured.get(key, "")
            if val:
                translated_val = self._translation_svc.translate(val)
                parts.append(f"  **{label}:** {translated_val}")
        return "\n".join(parts)

    def _render_emerging_topics(self, topics: Tuple[EmergingTopic, ...]) -> str:
        header = "## Conceptos emergentes (no alineados)\n\n"
        if not topics:
            return header + "No se detectaron conceptos emergentes.\n"
        header += (
            "Tópicos descubiertos automáticamente que no superaron el umbral "
            "de alineamiento.\n\n"
        )
        parts = [header]
        for i, topic in enumerate(topics, 1):
            ids_str = ", ".join(topic.paper_ids[:10])
            if len(topic.paper_ids) > 10:
                ids_str += " ..."
            parts.append(
                f"### Emergente {i}: {topic.title}\n"
                f"- **Palabras clave:** {', '.join(topic.keywords)}\n"
                f"- **Papers ({len(topic.paper_ids)}):** `{ids_str}`\n"
                f"- **Descripción:** {topic.description}\n"
            )
        return "\n".join(parts) + "\n---\n"

    def _render_manuscript_reference(self, ref) -> str:
        label = f"[{ref.bibkey}]" if ref.bibkey else "(sin clave)"
        title = ref.title or ref.bibkey
        block = [f"### {label} — {title}\n"]
        if ref.doi:
            block.append(f"*DOI: `{ref.doi}`*\n")

        if ref.contexts:
            block.append("**Cita en el manuscrito:**")
            for ctx in ref.contexts:
                block.append(f'> "{ctx}"')
            block.append("")
        else:
            block.append(
                "**Cita en el manuscrito:** *no se encontró contexto textual*\n"
            )

        if ref.extension:
            block.append(f"**Qué aportaba el paper original:** {ref.extension}\n")

        structured_md = self._render_structured_evidence(ref.structured_evidence)
        if structured_md:
            block.append(structured_md)
        return "\n".join(block)

    def _render_manuscript_evidence(self, evidence: ManuscriptEvidence) -> str:
        n_missing = len(evidence.cited_without_pdf)
        parts = [
            "## Evidencias en el manuscrito\n\n",
            "Referencias citadas en el manuscrito del investigador: se muestra el "
            "texto que las menciona, un recordatorio de su aportación original "
            "(para confirmar la relevancia) y sus datos estructurados.\n\n",
            f"**Resumen:** {evidence.n_cited} referencias citadas; "
            f"{evidence.n_matched} enriquecidas con el texto del paper; "
            f"{n_missing} sin PDF disponible (marcadas con ⚠️).\n\n",
        ]

        if not evidence.references and not n_missing:
            parts.append(
                "No se pudo asociar ninguna referencia citada con un PDF del corpus.\n"
            )

        for ref in evidence.references:
            parts.append(self._render_manuscript_reference(ref))

        # -- Referencias no enriquecidas: señaladas como susceptibles de error --
        if evidence.cited_without_pdf:
            parts.append("### ⚠️ Referencias citadas sin PDF disponible\n")
            parts.append(
                "Aparecen en el manuscrito pero no se ha podido asociar ningún PDF "
                "en `manual_papers/manuscript/` ni en el corpus. **Verificar la "
                "referencia**: la descarga está pendiente o la cita/bibkey puede ser "
                "errónea.\n"
            )
            for miss in evidence.cited_without_pdf:
                block = [f"#### ⚠️ [{miss.bibkey}] — {miss.title}\n"]
                if miss.doi:
                    block.append(f"*DOI: `{miss.doi}`*\n")
                if not miss.in_bib:
                    block.append(
                        f"⚠️ **No existe la entrada `{miss.bibkey}` en el fichero "
                        "`.bib` (posible error de cita).**\n"
                    )
                if miss.contexts:
                    block.append("**Cita en el manuscrito:**")
                    for ctx in miss.contexts:
                        block.append(f'> "{ctx}"')
                    block.append("")
                if miss.reason == "pdf_not_indexed":
                    block.append(
                        "**Estado:** ⚠️ el PDF está presente pero no se ha podido "
                        "indexar en el corpus (revisar el mapeo o reejecutar la ingesta).\n"
                    )
                else:
                    block.append(
                        "**Estado:** ⚠️ sin PDF (descarga pendiente o referencia errónea).\n"
                    )
                parts.append("\n".join(block))

        # -- PDFs presentes pero no asociados a ninguna bibkey --
        if evidence.unmatched_pdfs:
            parts.append("### ⚠️ PDFs sin referencia asociada\n")
            parts.append(
                "Ficheros presentes en `manual_papers/manuscript/` que no se han "
                "podido asociar a ninguna entrada de `biblio.bib` (revisar el nombre "
                "del fichero o la bibkey):\n"
            )
            for name in evidence.unmatched_pdfs:
                parts.append(f"- ⚠️ `{name}`")
            parts.append("")

        return "\n".join(parts) + "\n---\n"

    def _render_technical_annex(self, concepts: Tuple[Dict, ...]) -> str:
        header = "## Anexo Técnico: Metodologías y Experimentación\n\n"
        if not concepts:
            return header + "No se encontraron datos técnicos.\n"

        # Columnas derivadas dinámicamente de los campos configurados en
        # studyDescription.json (proyectados en cada perfil). Así el anexo es
        # genérico y no depende del dominio (antes: columnas fijas de sensores).
        columns = self._tech_columns(concepts)
        if not columns:
            return header + "No se encontraron datos técnicos.\n"

        table_header = "| ID | " + " | ".join(
            self._tech_label(c) for c in columns
        ) + " |"
        table_sep = "| :--- |" + " :--- |" * len(columns)

        lines = [header]
        for concept in concepts:
            papers = concept.get("papers", [])
            if not papers:
                continue
            lines.append(
                f"### Concepto {concept.get('concept_id')}: "
                f"{concept.get('concept_name', '')}\n"
            )
            lines.append(table_header)
            lines.append(table_sep)
            for paper in papers[:10]:
                lines.append(self._render_tech_row(paper, columns))
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _tech_columns(concepts: Tuple[Dict, ...]) -> Tuple[str, ...]:
        """Columnas = unión ordenada de las claves de perfil presentes."""
        columns: List[str] = []
        for concept in concepts:
            for paper in concept.get("papers", []):
                for key in (paper.get("profile") or {}).keys():
                    if key not in columns:
                        columns.append(key)
        return tuple(columns)

    @staticmethod
    def _tech_label(field: str) -> str:
        return field.replace("_", " ").strip().capitalize()

    @staticmethod
    def _render_tech_row(paper: Dict, columns: Tuple[str, ...]) -> str:
        profile = paper.get("profile", {})

        def fmt(val) -> str:
            if isinstance(val, list):
                return (", ".join(str(v) for v in val) or "—").replace("|", "\\|")
            return (str(val) if val not in (None, "") else "—").replace("|", "\\|")

        cells = " | ".join(fmt(profile.get(c)) for c in columns)
        return f"| `{paper.get('paper_id', '?')}` | {cells} |"

    # ------------------------------------------------------------------
    # BIBLIOGRAFÍA ENRIQUECIDA CON BIBTEX, DOI Y CITATION COUNT
    # ------------------------------------------------------------------

    def _render_bibliography(self, bibliography: Dict[str, Dict]) -> str:
        header = "## Referencias\n\n"
        if not bibliography:
            return header + "No se encontraron referencias.\n"

        concept = {k: v for k, v in bibliography.items() if v.get("group") == "concept"}
        emerging = {k: v for k, v in bibliography.items() if v.get("group") != "concept"}

        parts = [header]
        parts.append(f"### Incluidos en conceptos ({len(concept)})")
        parts.append(self._render_bib_group(concept))
        parts.append(f"\n### Solo en conceptos emergentes ({len(emerging)})")
        parts.append(self._render_bib_group(emerging))
        return "\n".join(parts)

    @staticmethod
    def _render_bib_group(entries: Dict[str, Dict]) -> str:
        if not entries:
            return "Ninguna.\n"
        ordered = sorted(entries.items(), key=lambda x: x[1].get("title", "").lower())
        lines = []
        for doc_id, info in ordered:
            bibtex = info.get("bibtex", "")
            if bibtex:
                lines.append(f"```bibtex\n{bibtex}\n```")
                continue
            fallback = f"- **{info.get('title', 'Unknown')}**  \n  (ID: `{doc_id}`)"
            if info.get("doi"):
                fallback += f", DOI: `{info['doi']}`"
            if info.get("citation_count"):
                fallback += f", Citas: {info['citation_count']}"
            lines.append(fallback + "\n")
        return "\n\n".join(lines)

    @staticmethod
    def _render_footer() -> str:
        return (
            "\n*Documento generado automáticamente por SLIP. "
            "Títulos y extractos en idioma original; datos estructurados traducidos al español.*\n"
        )

    def _evidence_strength(self, avg_score: float, count: int) -> Tuple[str, str]:
        for min_count, min_score, label, emoji in self._STRENGTH_MAP:
            if count >= min_count and avg_score >= min_score:
                return label, emoji
        return "Sin evidencia", "⚪"


# ---------------------------------------------------------------------------
# Capa 4 – Orquestador
# ---------------------------------------------------------------------------

class TrainingMaterialsPipeline:
    def __init__(
        self,
        builder: DocumentDataBuilder,
        renderer: MarkdownRenderer,
        translation_svc: TranslationService,
    ) -> None:
        self._builder = builder
        self._renderer = renderer
        self._translation_svc = translation_svc

    def run(
        self,
        findings_data: List[Dict],
        concepts_query: Dict,
        aligned_concepts: Dict,
        candidate_concepts: Dict,
        concepts_with_tech: List[Dict],
        papers_text: Dict,
        objectives_text: str,
        metadata_lookup: Dict[str, Dict],
        prisma_raw: Dict,
        output_file: Optional[Path] = None,
        cache_dir: Optional[Path] = None,
        manuscript_evidence: Optional[ManuscriptEvidence] = None,
    ) -> str:
        model = self._builder.build(
            findings_data=findings_data,
            concepts_query=concepts_query,
            aligned_concepts=aligned_concepts,
            candidate_concepts=candidate_concepts,
            concepts_with_tech=concepts_with_tech,
            papers_text=papers_text,
            objectives_text=objectives_text,
            metadata_lookup=metadata_lookup,
            prisma_raw=prisma_raw,
            manuscript_evidence=manuscript_evidence,
        )

        markdown = self._renderer.render(model)

        # La caché de traducción se nutre durante el render; guardarla después.
        if cache_dir:
            self._translation_svc.save_cache()

        if output_file:
            output_file.write_text(markdown, encoding="utf-8")
            writeLog("info", logger, f"[TrainingMaterials] Saved to {output_file}")

        return markdown


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_pipeline(
    config: TrainingConfig,
    translation_svc: TranslationService,
    llm_client: Optional[LLMClient] = None,
) -> TrainingMaterialsPipeline:
    client = llm_client or create_ollama_client(
        model=config.llm_model,
        max_retries=2,
        temperature=config.llm_temperature,
        max_tokens=config.llm_max_tokens,
    )
    emerging_analyzer = EmergingTopicAnalyzer(llm_client=client, config=config)
    builder = DocumentDataBuilder(
        config=config,
        emerging_analyzer=emerging_analyzer,
    )
    renderer = MarkdownRenderer(config=config, translation_svc=translation_svc)
    pipeline = TrainingMaterialsPipeline(
        builder=builder,
        renderer=renderer,
        translation_svc=translation_svc,
    )
    return pipeline


# ---------------------------------------------------------------------------
# Helpers de I/O
# ---------------------------------------------------------------------------

def _load_optional(path: Path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def processTrainingMaterials() -> Optional[str]:
    input_dir, output_dir = inicioModulo("processTrainingMaterials")

    # --- CARGAR DATOS ---
    findings_file = output_dir / "concept_findings.json"
    if not findings_file.exists():
        writeLog("error", logger, f"[TrainingMaterials] {findings_file} not found")
        return None

    findings_data = _load_optional(findings_file, [])
    candidate = _load_optional(
        output_dir / "candidate_concepts.json",
        {"concepts": [], "n_topics": 0},
    )
    aligned = _load_optional(
        output_dir / "aligned_concepts.json",
        {"aligned_concepts": [], "alignment_threshold": 0.30},
    )
    concepts_query = _load_optional(input_dir / "conceptsQuery.json", {"query": "", "concepts": []})
    tech_data = _load_optional(output_dir / "technical_annex.json", {})
    concepts_with_tech = tech_data.get("concepts", []) if tech_data else []

    papers_text: Dict = {}
    papers_file = output_dir / "papers_text.json"
    if candidate.get("concepts") and papers_file.exists():
        raw = _load_optional(papers_file, [])
        papers_text = {p["paper_id"]: p for p in raw if "paper_id" in p}

    # --- EXTRAER OBJETIVOS desde studyDescription.json ---
    study_data = {}
    study_file = input_dir / "studyDescription.json"
    if study_file.exists():
        study_data = _load_optional(study_file, {})
    objectives = study_data.get("project", {}).get("objectives", [])
    objectives_text = " ".join(objectives) if objectives else "No especificados."

    # --- EXTRAER METADATOS (DOI, citation_count, authors, year, venue) desde papers_metadata.json ---
    metadata_file = output_dir / "papers_metadata.json"
    metadata_lookup: Dict[str, Dict] = {}

    # canonical.json aporta authors/venue/doi/title completos que papers_metadata no conserva
    canonical_lookup: Dict[str, Dict] = {}
    canonical_file = output_dir / "canonical.json"
    if canonical_file.exists():
        for p in _load_optional(canonical_file, []):
            if "paper_id" in p:
                canonical_lookup[p["paper_id"]] = p

    if metadata_file.exists():
        papers_metadata = _load_optional(metadata_file, [])
        for p in papers_metadata:
            if "paper_id" not in p:
                continue
            pid = p["paper_id"]
            canon = canonical_lookup.get(pid, {})
            metadata_lookup[pid] = {
                "title": p.get("title") or canon.get("title", ""),
                "doi": p.get("doi") or canon.get("doi", ""),
                "abstract": p.get("abstract") or canon.get("abstract", ""),
                "citation_count": p.get("citation_count",
                                        p.get("cited_by_count", canon.get("citation_count", 0))),
                "authors": p.get("authors") or canon.get("authors", []),
                "year": p.get("year") or canon.get("year"),
                "venue": p.get("venue") or canon.get("venue", ""),
                "keywords": p.get("keywords") or canon.get("keywords", []),
            }

    # --- CONTEOS PRISMA brutos derivados del pipeline ---
    search_counts = {}
    for db, fname in (("WoS", "wos_search.json"), ("Scopus", "scopus_search.json"),
                      ("IEEE", "ieee_search.json"), ("PubMed", "pubmed_search.json")):
        search_counts[db] = len(_load_optional(output_dir / fname, []))
    canonical = _load_optional(output_dir / "canonical.json", [])
    selected = _load_optional(output_dir / "selected_acquired.json",
                              _load_optional(output_dir / "selected_papers.json", []))
    missing = _load_optional(output_dir / "missing_pdfs.json", [])

    prisma_raw = {
        "db_counts": search_counts,
        "n_duplicates": max(0, sum(search_counts.values()) - len(canonical)),
        "n_sought": len(selected),
        "n_not_retrieved": len(missing),
        "not_retrieved_papers": [{"paper_id": m.get("paper_id"), "title": m.get("title", "")}
                                 for m in missing],
    }

    # --- CONFIG ---
    config = TrainingConfig.from_file(study_file)
    config.emerging_enabled = emerging_enabled()
    config.manuscript_enabled = manuscript_enabled()
    writeLog("info", logger,
             f"[TrainingMaterials] Flags: manuscript={int(config.manuscript_enabled)}, "
             f"emergente={int(config.emerging_enabled)}")

    # --- CLIENTE LLM (compartido por tópicos emergentes y manuscrito) ---
    llm_client = create_ollama_client(
        model=config.llm_model,
        max_retries=2,
        temperature=config.llm_temperature,
        max_tokens=config.llm_max_tokens,
    )

    # --- EVIDENCIA DEL MANUSCRITO (opcional, --manuscript=1) ---
    manuscript_evidence = None
    if config.manuscript_enabled:
        refs = build_manuscript_refs(input_dir, output_dir, persist=True)
        if refs:
            enrichment_cfg = EnrichmentConfig.from_file(study_file)
            extractor = PaperSectionExtractor(enrichment_cfg)
            ms_builder = ManuscriptEvidenceBuilder(
                llm_client=llm_client,
                cache_file=output_dir / "manuscript_context_cache.json",
                extractor=extractor,
                max_tokens=config.manuscript_extension_max_tokens,
            )
            manuscript_evidence = ms_builder.build(refs, papers_text, metadata_lookup)
        else:
            writeLog("warning", logger,
                     "[TrainingMaterials] Manuscrito activado pero no se pudo construir "
                     "manuscript_refs.json (revisa manual_papers/manuscript/).")

    # --- TRADUCCIÓN ---
    cache_file = output_dir / config.translation_cache_file
    translation_svc = TranslationService(
        cache_file=cache_file,
        backend=config.translation_backend,
        model_name=config.translation_model,
    )
    translation_svc.load_cache()

    # --- PIPELINE ---
    pipeline = build_pipeline(config, translation_svc, llm_client=llm_client)

    return pipeline.run(
        findings_data=findings_data,
        concepts_query=concepts_query,
        aligned_concepts=aligned,
        candidate_concepts=candidate,
        concepts_with_tech=concepts_with_tech,
        papers_text=papers_text,
        objectives_text=objectives_text,
        metadata_lookup=metadata_lookup,
        prisma_raw=prisma_raw,
        output_file=output_dir / "training_materials.md",
        cache_dir=output_dir,
        manuscript_evidence=manuscript_evidence,
    )


if __name__ == "__main__":
    processTrainingMaterials()