"""
concept_evidence.py
====================
Extracción de evidencias textuales de papers académicos por concepto,
usando ventanas deslizantes, scoring híbrido (embedding + BM25) y
clusterización intra-documento para reducir redundancia.

Arquitectura en capas (SOA):
  ┌────────────────────────────────────────────────────────┐
  │         ConceptEvidencePipeline  (orquestador)         │
  ├──────────────┬──────────────────┬──────────────────────┤
  │ ModelRegistry│  WindowScorer    │  EvidenceFilter      │
  │ (modelos NLP)│  (scoring batch) │  (percentil+rescate) │
  ├──────────────┴──────────────────┴──────────────────────┤
  │              DocumentEvidenceExtractor                  │
  │   (ventanas → scoring → evidencias por documento)      │
  └────────────────────────────────────────────────────────┘

Principios aplicados:
  - SRP  : cada clase tiene una única responsabilidad
  - OCP  : umbrales y parámetros configurables sin tocar código
  - DIP  : modelos NLP inyectados, no globales
  - Rendimiento: encode() siempre en batch, nunca por ventana individual
  - Sin estado global mutable: ModelRegistry gestiona el ciclo de vida
  - QueryExpander integrado via llm_client federado (query_expander refactorizado)
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import spacy
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer
from sklearn.cluster import AgglomerativeClustering
from spacy.lang.en import English

from sources.common.common import logger, writeLog, processControl
from sources.common.utils import inicioModulo

# ---------------------------------------------------------------------------
# Objetos de configuración tipados (sin constantes globales sueltas)
# ---------------------------------------------------------------------------

@dataclass
class WindowConfig:
    """Parámetros de ventanas deslizantes."""
    size: int = 5
    step: int = 2
    min_sentences: int = 3
    min_words: int = 20


@dataclass
class ScoringConfig:
    """Pesos y umbrales del scoring híbrido."""
    embedding_weight: float = 0.7
    bm25_weight: float = 0.3
    claim_boost_factor: float = 0.05
    focus_term_boost: float = 0.15
    min_window_similarity: float = 0.05


@dataclass
class FilterConfig:
    """Umbrales de filtrado y límites de output."""
    similarity_percentile_cutoff: int = 30    # retiene el 70% superior
    similarity_absolute_floor: float = 0.30
    min_rescue_similarity: float = 0.50
    max_evidences_per_concept: int = 80
    intradoc_cluster_threshold: float = 0.40


@dataclass
class EvidenceConfig:
    """Configuración completa del módulo."""
    embedding_model_name: str = "BAAI/bge-base-en-v1.5"
    window: WindowConfig = field(default_factory=WindowConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    # Configuración del expansor de queries (específica del módulo)
    expansion_max_terms: int = 7
    expansion_enabled: bool = True
    # NOTA: expansion_llm_model eliminado. Ahora se lee de processControl.defaults.llm

    @classmethod
    def from_project_config(cls, project: Dict) -> "EvidenceConfig":
        """Construye la config desde el bloque 'project' de studyDescription.json."""
        ev_cfg = project.get("evidence_extraction", {})
        exp_cfg = project.get("query_expansion", {})
        return cls(
            embedding_model_name=ev_cfg.get("embedding_model", "BAAI/bge-base-en-v1.5"),
            window=WindowConfig(
                size=ev_cfg.get("window_size", 5),
                step=ev_cfg.get("window_step", 2),
                min_sentences=ev_cfg.get("min_sentences", 3),
                min_words=ev_cfg.get("min_window_words", 20),
            ),
            scoring=ScoringConfig(
                embedding_weight=ev_cfg.get("embedding_weight", 0.7),
                bm25_weight=ev_cfg.get("bm25_weight", 0.3),
                claim_boost_factor=ev_cfg.get("claim_boost_factor", 0.05),
                focus_term_boost=ev_cfg.get("focus_term_boost", 0.15),
                min_window_similarity=ev_cfg.get("min_window_similarity", 0.05),
            ),
            filter=FilterConfig(
                similarity_percentile_cutoff=ev_cfg.get("similarity_percentile_cutoff", 30),
                similarity_absolute_floor=ev_cfg.get("similarity_absolute_floor", 0.30),
                min_rescue_similarity=ev_cfg.get("min_rescue_similarity", 0.50),
                max_evidences_per_concept=ev_cfg.get("max_evidences_per_concept", 80),
                intradoc_cluster_threshold=ev_cfg.get("intradoc_cluster_threshold", 0.40),
            ),
            expansion_max_terms=exp_cfg.get("max_expansion_terms", 7),
            expansion_enabled=exp_cfg.get("enabled", True),
        )

    @classmethod
    def from_file(cls, study_file: Path) -> "EvidenceConfig":
        if not study_file.exists():
            writeLog("warning", logger,
                     f"[EvidenceConfig] studyDescription.json not found: {study_file}. Using defaults.")
            return cls()
        with open(study_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_project_config(data.get("project", {}))


# ---------------------------------------------------------------------------
# Objetos de dominio
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    """Evidencia extraída de un documento. Inmutable."""
    doc_id: str
    paper_title: str
    evidence_text: str
    similarity: float

    def to_dict(self) -> Dict:
        return {
            "doc_id": self.doc_id,
            "paper_title": self.paper_title,
            "evidence_text": self.evidence_text,
            "similarity": round(self.similarity, 4),
        }


@dataclass(frozen=True)
class ConceptEvidenceResult:
    """Resultado de extracción para un concepto. Inmutable."""
    concept_id: str
    concept_query: str
    knowledge_units: List[Evidence]

    def to_dict(self) -> Dict:
        return {
            "concept_id": self.concept_id,
            "concept_query": self.concept_query,
            "knowledge_units": [e.to_dict() for e in self.knowledge_units],
        }


# ---------------------------------------------------------------------------
# Capa 1 – Registro de modelos (gestiona ciclo de vida, no globals)
# ---------------------------------------------------------------------------

class ModelRegistry:
    """
    Carga y retiene los modelos NLP de forma lazy y controlada.
    Reemplaza el patrón de globals mutables (_nlp_sent, _embedding_model).
    Se instancia una vez y se inyecta donde se necesita.
    """

    def __init__(self, embedding_model_name: str) -> None:
        self._embedding_model_name = embedding_model_name
        self._nlp: Optional[English] = None
        self._embedder: Optional[SentenceTransformer] = None

    def load(self) -> None:
        """Carga ambos modelos. Idempotente: no recarga si ya están en memoria."""
        if self._nlp is None:
            writeLog("info", logger,
                     "[ModelRegistry] Loading spaCy sentencizer...")
            self._nlp = English()
            self._nlp.add_pipe("sentencizer")

        if self._embedder is None:
            writeLog("info", logger,
                     f"[ModelRegistry] Loading embedding model: {self._embedding_model_name}")
            self._embedder = SentenceTransformer(self._embedding_model_name)

    @property
    def nlp(self) -> English:
        if self._nlp is None:
            raise RuntimeError("ModelRegistry.load() must be called before accessing .nlp")
        return self._nlp

    @property
    def embedder(self) -> SentenceTransformer:
        if self._embedder is None:
            raise RuntimeError("ModelRegistry.load() must be called before accessing .embedder")
        return self._embedder

    def encode_batch(self, texts: List[str], normalize: bool = True) -> np.ndarray:
        """
        Codifica una lista de textos en batch.
        SIEMPRE usar este método en lugar de llamar a embedder.encode() directamente,
        para garantizar que nunca se encode de uno en uno en bucles.
        """
        return self.embedder.encode(
            texts,
            normalize_embeddings=normalize,
            batch_size=32,
            show_progress_bar=False,
        )


# ---------------------------------------------------------------------------
# Capa 2 – Utilidades de texto (funciones puras, sin estado)
# ---------------------------------------------------------------------------

class TextUtils:
    """Funciones puras de procesamiento de texto. Sin estado."""

    _NOISE_PATTERNS = (
        "copyright", "permission", "proceedings",
        "association for computational linguistics",
        "doi:", "http://", "https://", "www.",
        "et al.", "fig.", "table", "reference",
    )
    _CLAIM_PATTERNS = (
        "result", "find", "show", "demonstrate", "evaluate",
        "outperform", "fail", "improve", "achieve", "significantly",
        "conclusion", "propose", "validate", "performance",
    )

    @staticmethod
    def split_sentences(text: str, nlp: English) -> List[str]:
        if not text:
            return []
        doc = nlp(text)
        return [s.text.strip() for s in doc.sents if s.text.strip()]

    @staticmethod
    def create_sliding_windows(sentences: List[str], cfg: WindowConfig) -> List[str]:
        n = len(sentences)
        if n < cfg.size:
            return [" ".join(sentences)] if sentences else []
        windows: List[str] = []
        for start in range(0, n - cfg.size + 1, cfg.step):
            windows.append(" ".join(sentences[start: start + cfg.size]))
        # Última ventana si no fue incluida exactamente
        last = " ".join(sentences[-cfg.size:])
        if last not in windows:
            windows.append(last)
        return windows

    @classmethod
    def is_noise(cls, text: str, min_words: int) -> bool:
        lower = text.lower()
        if any(p in lower for p in cls._NOISE_PATTERNS) and len(text.split()) < 40:
            return True
        return len(text.split()) < min_words

    @classmethod
    def claim_score(cls, text: str, focus_terms: List[str]) -> int:
        lower = text.lower()
        score = sum(p in lower for p in cls._CLAIM_PATTERNS)
        if focus_terms:
            score += min(sum(1 for t in focus_terms if t.lower() in lower), 5)
        return score

    @staticmethod
    def tokenize_bm25(text: str) -> List[str]:
        return text.lower().split()

    @staticmethod
    def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))

    @staticmethod
    def build_query(concept: Dict) -> str:
        if concept.get("concept_query"):
            return concept["concept_query"]
        parts = [concept.get("concept_name", ""), concept.get("concept_description", "")]
        return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Capa 3 – Scoring de ventanas (BATCH, no por ventana individual)
# ---------------------------------------------------------------------------

class WindowScorer:
    """
    Calcula scores híbridos (embedding + BM25 + boost) para un conjunto
    de ventanas frente a las queries de un concepto.

    Principio clave de rendimiento: todos los encodes se hacen en UN SOLO
    batch por documento, no ventana a ventana.
    """

    def __init__(self, registry: ModelRegistry, config: ScoringConfig) -> None:
        self._registry = registry
        self._cfg = config

    def score_windows(
        self,
        windows: List[str],
        query_embeddings: np.ndarray,   # shape: (n_queries, emb_dim)
        focus_terms: List[str],
    ) -> List[Tuple[str, float]]:
        """
        Devuelve lista de (window_text, final_score) para todas las ventanas.

        El encode de ventanas es un ÚNICO batch por llamada.
        query_embeddings se precalcula una vez por concepto (ver _get_query_embeddings).
        """
        if not windows:
            return []

        # ── Batch encode de TODAS las ventanas de este documento ──────────────
        window_embeddings = self._registry.encode_batch(windows)  # (n_windows, emb_dim)

        # ── BM25 sobre el corpus de ventanas ─────────────────────────────────
        tokenized_corpus = [TextUtils.tokenize_bm25(w) for w in windows]
        bm25 = BM25Okapi(tokenized_corpus)
        # BM25 usa la primera query (primaria) para el score base
        primary_tokens = TextUtils.tokenize_bm25(
            windows[0] if not windows else ""   # placeholder; se sobreescribe abajo
        )
        # Calculamos scores BM25 para la primera query embeddings
        # (query_embeddings[0] corresponde a la query primaria)
        # Necesitamos el texto de la query; lo pasamos por separado
        raw_bm25 = np.zeros(len(windows))      # se rellena en score_all

        results: List[Tuple[str, float]] = []
        for i, (window_text, w_emb) in enumerate(zip(windows, window_embeddings)):
            # Similitud de embedding: máxima entre todas las queries
            sims = np.dot(query_embeddings, w_emb)   # (n_queries,) — ya normalizados
            max_sim = float(sims.max())

            # BM25 normalizado (se calcula en batch más abajo)
            # Se añade en _score_all; aquí usamos max_sim como proxy temporal
            final = self._compute_final(
                max_sim=max_sim,
                bm25_norm=raw_bm25[i],          # 0 en primer paso; se actualiza
                window_text=window_text,
                focus_terms=focus_terms,
            )
            results.append((window_text, final, max_sim))

        return results  # (text, final_score, embedding_score)

    def score_windows_full(
        self,
        windows: List[str],
        query_embeddings: np.ndarray,
        primary_query_tokens: List[str],
        focus_terms: List[str],
    ) -> List[Tuple[str, float, float]]:
        """
        Versión completa con BM25 correctamente normalizado.
        Devuelve (window_text, final_score, embedding_score).
        """
        if not windows:
            return []

        # ── Batch encode de ventanas (UNA sola llamada) ───────────────────────
        window_embeddings = self._registry.encode_batch(windows)   # (n_win, dim)

        # ── BM25 ──────────────────────────────────────────────────────────────
        tokenized_corpus = [TextUtils.tokenize_bm25(w) for w in windows]
        bm25 = BM25Okapi(tokenized_corpus)
        raw_bm25 = np.array(bm25.get_scores(primary_query_tokens), dtype=float)

        bm25_min, bm25_max = raw_bm25.min(), raw_bm25.max()
        norm_bm25 = (
            (raw_bm25 - bm25_min) / (bm25_max - bm25_min)
            if bm25_max - bm25_min > 1e-8
            else np.zeros_like(raw_bm25)
        )

        # ── Similitudes de embedding (operación matricial, no bucle) ──────────
        # query_embeddings: (n_queries, dim), window_embeddings: (n_win, dim)
        sim_matrix = window_embeddings @ query_embeddings.T    # (n_win, n_queries)
        max_sims = sim_matrix.max(axis=1)                      # (n_win,)

        # ── Score final por ventana ───────────────────────────────────────────
        results: List[Tuple[str, float, float]] = []
        for i, window_text in enumerate(windows):
            final = self._compute_final(
                max_sim=float(max_sims[i]),
                bm25_norm=float(norm_bm25[i]),
                window_text=window_text,
                focus_terms=focus_terms,
            )
            results.append((window_text, final, float(max_sims[i])))

        return results

    def _compute_final(
        self,
        max_sim: float,
        bm25_norm: float,
        window_text: str,
        focus_terms: List[str],
    ) -> float:
        c = self._cfg
        hybrid = c.embedding_weight * max_sim + c.bm25_weight * bm25_norm
        claim = TextUtils.claim_score(window_text, [])
        final = hybrid + c.claim_boost_factor * claim

        if focus_terms:
            lower = window_text.lower()
            hits = sum(1 for t in focus_terms if t.lower() in lower)
            final += hits * c.focus_term_boost

        return final


# ---------------------------------------------------------------------------
# Capa 4 – Filtrado y clustering (sin dependencia de modelos NLP)
# ---------------------------------------------------------------------------

class EvidenceFilter:
    """
    Aplica filtros de calidad sobre un conjunto de evidencias:
      1. Piso absoluto de similitud
      2. Filtro de percentil global
      3. Rescate de documentos sin representación (con umbral mínimo)
      4. Límite de evidencias por concepto
    """

    def __init__(self, config: FilterConfig) -> None:
        self._cfg = config

    def apply(
        self,
        evidences: List[Evidence],
        all_doc_evidences: Dict[str, List[Evidence]],
    ) -> List[Evidence]:
        """
        Aplica el pipeline de filtrado completo.

        Args:
            evidences:         Lista global de evidencias (ya deduplicadas).
            all_doc_evidences: Mapa doc_id → evidencias del doc (para rescate).
        """
        # Paso 1: percentil con piso absoluto
        filtered = self._percentile_filter(evidences)

        # Paso 2: rescate controlado de documentos sin representación
        filtered = self._rescue_missing_docs(filtered, all_doc_evidences)

        # Paso 3: límite global
        filtered.sort(key=lambda e: e.similarity, reverse=True)
        return filtered[: self._cfg.max_evidences_per_concept]

    def _percentile_filter(self, evidences: List[Evidence]) -> List[Evidence]:
        above_floor = [e for e in evidences
                       if e.similarity >= self._cfg.similarity_absolute_floor]
        if not above_floor:
            return []
        scores = np.array([e.similarity for e in above_floor])
        threshold = float(np.percentile(scores, self._cfg.similarity_percentile_cutoff))
        return [e for e in above_floor if e.similarity >= threshold]

    def _rescue_missing_docs(
        self,
        filtered: List[Evidence],
        all_doc_evidences: Dict[str, List[Evidence]],
    ) -> List[Evidence]:
        represented = {e.doc_id for e in filtered}
        rescued = list(filtered)

        for doc_id, evs in all_doc_evidences.items():
            if doc_id in represented or not evs:
                continue
            best = evs[0]   # ya ordenadas por similitud desc
            if best.similarity >= self._cfg.min_rescue_similarity:
                rescued.append(best)
                represented.add(doc_id)
                writeLog("debug", logger,
                         f"[EvidenceFilter] Rescued doc {doc_id} "
                         f"(sim={best.similarity:.3f})")
            else:
                writeLog("debug", logger,
                         f"[EvidenceFilter] Doc {doc_id} NOT rescued "
                         f"(sim={best.similarity:.3f} < {self._cfg.min_rescue_similarity})")
        return rescued


class IntraDocClusterer:
    """
    Clusteriza evidencias de un mismo documento y retiene la mejor por cluster,
    reduciendo la redundancia semántica intra-documento.
    """

    def __init__(self, registry: ModelRegistry, threshold: float) -> None:
        self._registry = registry
        self._threshold = threshold

    def cluster(self, evidences: List[Evidence]) -> List[Evidence]:
        if len(evidences) <= 1:
            return evidences

        texts = [e.evidence_text for e in evidences]
        # Batch encode de las evidencias del documento
        embeddings = self._registry.encode_batch(texts)

        clustering = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=self._threshold,
            metric="cosine",
            linkage="average",
        )
        labels = clustering.fit_predict(embeddings)

        clusters: Dict[int, List[Evidence]] = defaultdict(list)
        for ev, label in zip(evidences, labels):
            clusters[label].append(ev)

        return [max(evs, key=lambda e: e.similarity) for evs in clusters.values()]


# ---------------------------------------------------------------------------
# Capa 5 – Extractor por documento
# ---------------------------------------------------------------------------

class DocumentEvidenceExtractor:
    """
    Extrae evidencias de un único documento para un concepto dado.
    Orquesta: ventanas → filtro de ruido → score batch → Evidence objects.
    """

    def __init__(
        self,
        registry: ModelRegistry,
        scorer: WindowScorer,
        config: EvidenceConfig,
    ) -> None:
        self._registry = registry
        self._scorer = scorer
        self._cfg = config

    def extract(
        self,
        concept: Dict,
        document: Dict,
        query_embeddings: np.ndarray,
        primary_query_tokens: List[str],
        focus_terms: List[str],
    ) -> List[Evidence]:
        """
        Extrae evidencias de un documento.
        query_embeddings y primary_query_tokens se precalculan fuera
        (una vez por concepto) y se reutilizan para todos los documentos.
        """
        text = document.get("text", "")
        sentences = TextUtils.split_sentences(text, self._registry.nlp)

        if len(sentences) < self._cfg.window.min_sentences:
            return []

        windows = TextUtils.create_sliding_windows(sentences, self._cfg.window)
        windows = [w for w in windows
                   if not TextUtils.is_noise(w, self._cfg.window.min_words)]
        if not windows:
            return []

        scored = self._scorer.score_windows_full(
            windows=windows,
            query_embeddings=query_embeddings,
            primary_query_tokens=primary_query_tokens,
            focus_terms=focus_terms,
        )

        return [
            Evidence(
                doc_id=document["doc_id"],
                paper_title=document.get("title", ""),
                evidence_text=text,
                similarity=score,
            )
            for text, score, _emb_score in scored
            if score >= self._cfg.scoring.min_window_similarity
        ]


# ---------------------------------------------------------------------------
# Capa 6 – Cache de embeddings de queries (funcional, sin global)
# ---------------------------------------------------------------------------

class QueryEmbeddingCache:
    """
    Cache LRU de embeddings de queries, asociado a un ModelRegistry concreto.
    Evita reencodeado de queries repetidas entre documentos del mismo concepto.
    No usa globals: el estado está encapsulado en la instancia.
    """

    def __init__(self, registry: ModelRegistry, maxsize: int = 128) -> None:
        self._registry = registry
        # lru_cache sobre método de instancia requiere este patrón
        self._cache: Dict[Tuple, np.ndarray] = {}
        self._maxsize = maxsize

    def get(self, queries: Tuple[str, ...]) -> np.ndarray:
        if queries not in self._cache:
            if len(self._cache) >= self._maxsize:
                # Eviction simple: eliminar la entrada más antigua
                oldest = next(iter(self._cache))
                del self._cache[oldest]
            self._cache[queries] = self._registry.encode_batch(list(queries))
        return self._cache[queries]


# ---------------------------------------------------------------------------
# Capa 7 – Procesador de concepto
# ---------------------------------------------------------------------------

class ConceptProcessor:
    """
    Procesa un concepto completo:
    precalcula query embeddings → extrae evidencias por documento →
    deduplica → clusteriza → filtra → devuelve ConceptEvidenceResult.
    """

    def __init__(
        self,
        extractor: DocumentEvidenceExtractor,
        clusterer: IntraDocClusterer,
        evidence_filter: EvidenceFilter,
        query_cache: QueryEmbeddingCache,
    ) -> None:
        self._extractor = extractor
        self._clusterer = clusterer
        self._filter = evidence_filter
        self._cache = query_cache

    def process(self, concept: Dict, focus_terms: List[str]) -> ConceptEvidenceResult:
        concept_id = str(concept.get("concept_id", "unknown"))
        primary_query = TextUtils.build_query(concept)
        expanded = concept.get("expanded_queries", [])
        all_queries = tuple([primary_query] + expanded)

        # Precalcular embeddings de todas las queries UNA VEZ para este concepto
        query_embeddings = self._cache.get(all_queries)   # (n_queries, dim)
        primary_tokens = TextUtils.tokenize_bm25(primary_query)

        # Extraer evidencias por documento
        all_evidences: List[Evidence] = []
        doc_evidences_map: Dict[str, List[Evidence]] = {}

        for document in concept.get("documents", []):
            doc_id = document.get("doc_id", "unknown")
            evs = self._extractor.extract(
                concept=concept,
                document=document,
                query_embeddings=query_embeddings,
                primary_query_tokens=primary_tokens,
                focus_terms=focus_terms,
            )
            # Deduplicar por texto exacto dentro del documento
            seen_texts: set = set()
            unique_evs: List[Evidence] = []
            for ev in evs:
                if ev.evidence_text not in seen_texts:
                    seen_texts.add(ev.evidence_text)
                    unique_evs.append(ev)

            # Clusterizar intra-documento
            if len(unique_evs) > 1:
                before = len(unique_evs)
                unique_evs = self._clusterer.cluster(unique_evs)
                if before > len(unique_evs):
                    writeLog("debug", logger,
                             f"[ConceptProcessor] Concept {concept_id}, doc {doc_id}: "
                             f"{before} → {len(unique_evs)} after clustering")

            # Ordenar por similitud desc
            unique_evs.sort(key=lambda e: e.similarity, reverse=True)
            doc_evidences_map[doc_id] = unique_evs
            all_evidences.extend(unique_evs)

        # Filtro global (percentil + rescate + límite)
        all_evidences.sort(key=lambda e: e.similarity, reverse=True)
        final_evidences = self._filter.apply(all_evidences, doc_evidences_map)

        writeLog("info", logger,
                 f"[ConceptProcessor] Concept {concept_id}: "
                 f"{len(final_evidences)} evidences from "
                 f"{len(set(e.doc_id for e in final_evidences))} docs")

        return ConceptEvidenceResult(
            concept_id=concept_id,
            concept_query=primary_query,
            knowledge_units=final_evidences,
        )


# ---------------------------------------------------------------------------
# Capa 8 – Integración con QueryExpander (via llm_client federado)
# ---------------------------------------------------------------------------

def _expand_queries(
    aligned_concepts: Dict,
    config: EvidenceConfig,
) -> Dict:
    if not config.expansion_enabled:
        writeLog("info", logger, "[Evidence] Query expansion disabled")
        return aligned_concepts

    try:
        from sources.queryExpander import QueryExpander, QueryExpanderConfig

        # MAGIA: Ya NO pasamos llm_model. QueryExpander lo leerá de processControl por debajo.
        exp_config = QueryExpanderConfig(
            enabled=True,
            max_expansion_terms=config.expansion_max_terms,
        )
        expander = QueryExpander(config=exp_config)
        expanded = expander.expand_all(aligned_concepts)
        writeLog("info", logger, "[Evidence] Query expansion completed")
        return expanded

    except ImportError:
        writeLog("warning", logger,
                 "[Evidence] QueryExpander not available, continuing without expansion")
    except Exception as exc:
        writeLog("warning", logger,
                 f"[Evidence] Query expansion failed: {exc}, continuing without expansion")

    return aligned_concepts


# ---------------------------------------------------------------------------
# Capa 9 – Orquestador del pipeline
# ---------------------------------------------------------------------------

class ConceptEvidencePipeline:
    """
    Orquesta el pipeline completo: carga → expansión → extracción → guardado.
    Recibe todas las dependencias por inyección.
    """

    def __init__(self, processor: ConceptProcessor, config: EvidenceConfig) -> None:
        self._processor = processor
        self._config = config

    def run(
        self,
        aligned_concepts: Dict,
        focus_terms: List[str],
        output_file: Optional[Path] = None,
    ) -> Dict:
        """
        Ejecuta el pipeline completo y devuelve el resultado serializable.
        Si output_file se especifica, guarda el resultado en disco.
        """
        results: List[ConceptEvidenceResult] = []

        for concept in aligned_concepts.get("aligned_concepts", []):
            writeLog("info", logger,
                     f"[Evidence] Processing concept {concept.get('concept_id')}")
            results.append(self._processor.process(concept, focus_terms))

        output = {
            "schema_version": "2.0",
            "embedding_model": self._config.embedding_model_name,
            "focus_terms_used": focus_terms,
            "concepts": [r.to_dict() for r in results],
        }

        if output_file:
            with open(output_file, "w", encoding="utf-8") as fh:
                json.dump(output, fh, indent=2, ensure_ascii=False)
            writeLog("info", logger, f"[Evidence] Saved to {output_file}")

        self._log_summary(results)
        return output

    @staticmethod
    def _log_summary(results: List[ConceptEvidenceResult]) -> None:
        """Logging de resumen separado de la lógica de pipeline."""
        separator = "=" * 80
        writeLog("info", logger, separator)
        writeLog("info", logger, "RESUMEN DE CONCEPTOS CON EVIDENCIAS (knowledge_units)")
        writeLog("info", logger, separator)

        total = 0
        for r in results:
            n = len(r.knowledge_units)
            total += n
            doc_ids = sorted({e.doc_id for e in r.knowledge_units})
            query_short = r.concept_query[:80] + ("..." if len(r.concept_query) > 80 else "")
            writeLog("info", logger,
                     f"Concepto {r.concept_id}: '{query_short}' | "
                     f"{n} units | {len(doc_ids)} docs")

        writeLog("info", logger, "-" * 80)
        writeLog("info", logger, f"Total knowledge units: {total}")
        writeLog("info", logger, separator)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_pipeline(config: EvidenceConfig) -> ConceptEvidencePipeline:
    """
    Ensambla el pipeline con todas sus dependencias.
    Punto único de construcción del grafo de objetos.
    """
    registry = ModelRegistry(config.embedding_model_name)
    registry.load()

    scorer = WindowScorer(registry=registry, config=config.scoring)
    clusterer = IntraDocClusterer(registry=registry,
                                  threshold=config.filter.intradoc_cluster_threshold)
    evidence_filter = EvidenceFilter(config=config.filter)
    query_cache = QueryEmbeddingCache(registry=registry)

    extractor = DocumentEvidenceExtractor(
        registry=registry, scorer=scorer, config=config
    )
    processor = ConceptProcessor(
        extractor=extractor,
        clusterer=clusterer,
        evidence_filter=evidence_filter,
        query_cache=query_cache,
    )

    return ConceptEvidencePipeline(processor=processor, config=config)


# ---------------------------------------------------------------------------
# Helpers de I/O
# ---------------------------------------------------------------------------

def _load_focus_terms(input_dir: Path) -> List[str]:
    json_file = input_dir / "studyDescription.json"
    if not json_file.exists():
        return []
    try:
        with open(json_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("project", {}).get("focus_terms", [])
    except Exception as exc:
        writeLog("error", logger, f"[Evidence] Error loading focus_terms: {exc}")
        return []


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def processConceptEvidence() -> Optional[Dict]:
    """
    Punto de entrada del módulo.
    Mantiene la misma firma que la versión anterior para compatibilidad.
    """
    input_dir, output_dir = inicioModulo("processConceptEvidence")
    input_file = output_dir / "aligned_concepts.json"

    if not input_file.exists():
        writeLog("error", logger, f"[Evidence] {input_file} not found")
        return None

    with open(input_file, "r", encoding="utf-8") as fh:
        aligned_concepts = json.load(fh)

    config = EvidenceConfig.from_file(input_dir / "studyDescription.json")
    focus_terms = _load_focus_terms(input_dir)

    writeLog("info", logger,
             f"[Evidence] Focus terms: {len(focus_terms)} | "
             f"Expansion: {'enabled' if config.expansion_enabled else 'disabled'}")

    # Expansión de queries via llm_client federado (lee LLM de processControl)
    aligned_concepts = _expand_queries(aligned_concepts, config)

    # Construir y ejecutar pipeline
    pipeline = build_pipeline(config)
    return pipeline.run(
        aligned_concepts=aligned_concepts,
        focus_terms=focus_terms,
        output_file=output_dir / "concept_evidence.json",
    )


if __name__ == "__main__":
    processConceptEvidence()