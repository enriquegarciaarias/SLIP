# discoveryEngine.py
from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo

import json
from pathlib import Path
import re
from typing import List, Tuple, Optional, Dict, Set
from collections import Counter

import numpy as np
from sentence_transformers import SentenceTransformer

import hdbscan
from hdbscan import HDBSCAN
from sklearn.feature_extraction import _stop_words
from umap import UMAP
from bertopic import BERTopic
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.cluster import KMeans

# --------------------------------------------------
# SPACY: load or download model (once)
# --------------------------------------------------
try:
    import spacy

    nlp = spacy.load("en_core_web_sm", disable=["ner", "parser"])
    nlp.max_length = 2_000_000
except OSError:
    import subprocess

    writeLog("info", logger, "[Discovery] spaCy model not found. Downloading en_core_web_sm...")
    subprocess.run(["python", "-m", "spacy", "download", "en_core_web_sm"])
    import spacy

    nlp = spacy.load("en_core_web_sm", disable=["ner", "parser"])
    nlp.max_length = 2_000_000
except ImportError:
    writeLog("error", logger, "[Discovery] spaCy not installed.")
    raise

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

# --------------------------------------------------
# CONFIG (valores por defecto; sobrescribibles desde config.json ->
# processControl.defaults["discovery"], ver load_discovery_config())
# --------------------------------------------------

EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
# Cribado de secciones (comportamiento original)
SECTION_FILTER_THRESHOLD = 0.30
SECTION_FILTER_ALPHA = 0.5
# Filtro de conceptos off-topic: umbral base + ajuste dinámico por RQ que
# garantiza al menos MIN_TOPICS_PER_RQ topics con MIN_PAPERS_PER_TOPIC papers
OFF_TOPIC_THRESHOLD = 0.60
OFF_TOPIC_ALPHA = 0.5
MIN_TOPICS_PER_RQ = 2
MIN_PAPERS_PER_TOPIC = 2
N_SEED_TERMS = 30
RELEVANT_SECTIONS = ["abstract", "introduction", "methodology", "results", "conclusion"]

# Síntesis del contexto de estudio (ancla de similitud coseno)
SYNTHESIS_MIN_TOKENS = 300
SYNTHESIS_MAX_TOKENS = 600
SYNTHESIS_MAX_CHARS = 4000


def load_discovery_config() -> None:
    """
    Sobrescribe las constantes del módulo con la sección "discovery" de
    config.json, accesible vía processControl.defaults (cargada en
    sources/common/paramsManager.py -> manageDefaults()).
    Los valores del fichero tienen prioridad; si faltan, se conservan
    los valores por defecto definidos arriba.
    """
    defaults = getattr(processControl, "defaults", None) or {}
    if not isinstance(defaults, dict):
        defaults = {}
    disc = defaults.get("discovery", {})
    if not isinstance(disc, dict):
        disc = {}

    global EMBEDDING_MODEL, SECTION_FILTER_THRESHOLD, SECTION_FILTER_ALPHA, \
        OFF_TOPIC_THRESHOLD, OFF_TOPIC_ALPHA, MIN_TOPICS_PER_RQ, MIN_PAPERS_PER_TOPIC, \
        N_SEED_TERMS, SYNTHESIS_MIN_TOKENS, SYNTHESIS_MAX_TOKENS, SYNTHESIS_MAX_CHARS

    EMBEDDING_MODEL = disc.get("embedding_model", EMBEDDING_MODEL)
    SECTION_FILTER_THRESHOLD = float(disc.get("section_filter_threshold", SECTION_FILTER_THRESHOLD))
    SECTION_FILTER_ALPHA = float(disc.get("section_filter_alpha", SECTION_FILTER_ALPHA))
    OFF_TOPIC_THRESHOLD = float(disc.get("off_topic_threshold", OFF_TOPIC_THRESHOLD))
    OFF_TOPIC_ALPHA = float(disc.get("off_topic_alpha", OFF_TOPIC_ALPHA))
    MIN_TOPICS_PER_RQ = int(disc.get("min_topics_per_rq", MIN_TOPICS_PER_RQ))
    MIN_PAPERS_PER_TOPIC = int(disc.get("min_papers_per_topic", MIN_PAPERS_PER_TOPIC))
    N_SEED_TERMS = int(disc.get("n_seed_terms", N_SEED_TERMS))
    SYNTHESIS_MIN_TOKENS = int(disc.get("synthesis_min_tokens", SYNTHESIS_MIN_TOKENS))
    SYNTHESIS_MAX_TOKENS = int(disc.get("synthesis_max_tokens", SYNTHESIS_MAX_TOKENS))
    SYNTHESIS_MAX_CHARS = int(disc.get("synthesis_max_chars", SYNTHESIS_MAX_CHARS))

# UMAP / HDBSCAN
UMAP_COMPONENTS = 8
MIN_CLUSTER_SIZE = 3
MAX_TEXT_LENGTH = 500_000

# NUEVO: Número de papers a pre-seleccionar por cada RQ
TOP_K_PAPERS_PER_RQ = 40

EXTRA_STOPWORDS = {
    "december", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november",
    "2020", "2021", "2022", "2023", "2024", "2025", "2026",
    "vol", "pp", "doi", "crossref", "conference", "proceedings",
    "international", "workshop", "symposium", "et", "al", "ieee",
    "springer", "elsevier", "article", "press", "available", "online",
    "page", "pages", "figure", "table", "equation", "section",
    "appendix", "reference", "references", "acknowledgment", "acknowledgements"
}
DEFAULT_STOPS = set(_stop_words.ENGLISH_STOP_WORDS)
ALL_STOPWORDS = DEFAULT_STOPS.union(EXTRA_STOPWORDS)


# --------------------------------------------------
# CUSTOM VECTORIZER (Sin cambios, es perfecto)
# --------------------------------------------------
class POSFilterVectorizer(CountVectorizer):
    def __init__(self, nlp_model, ngram_range=(1, 2), min_df=2, max_df=0.85, max_features=5000, **kwargs):
        super().__init__(ngram_range=ngram_range, min_df=min_df, max_df=max_df, max_features=max_features, **kwargs)
        self.nlp_model = nlp_model

    def build_tokenizer(self):
        def tokenize(text):
            if len(text) > MAX_TEXT_LENGTH: text = text[:MAX_TEXT_LENGTH]
            doc = self.nlp_model(text)
            tokens = []
            for token in doc:
                if token.pos_ in ("NOUN", "ADJ", "PROPN"):
                    if (not token.is_stop and not token.like_num and len(token.text) > 2 and not any(
                            c.isdigit() for c in token.text)):
                        lemma = token.lemma_.lower()
                        if lemma not in EXTRA_STOPWORDS and len(lemma) > 2: tokens.append(lemma)
            return tokens

        return tokenize


def create_custom_vectorizer() -> CountVectorizer:
    return POSFilterVectorizer(nlp_model=nlp, ngram_range=(1, 2), min_df=2, max_df=0.85, max_features=5000)


# --------------------------------------------------
# ORIENTATION (Sin cambios lógicos, solo carga conceptsQuery)
# --------------------------------------------------
def load_study_context(input_dir: Path) -> tuple[str, dict, list[str]]:
    json_file = input_dir / "studyDescription.json"
    study_text, focus_terms, concepts_query = "", [], {}
    if json_file.exists():
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            project = data.get("project", {})
            study_text = " ".join(project.get("objectives", []))
            focus_terms = project.get("focus_terms", [])
            writeLog("info", logger,
                     f"[Discovery] Loaded studyDescription.json: {len(project.get('objectives', []))} objectives")
        except Exception as e:
            writeLog("error", logger, f"[Discovery] Error reading studyDescription.json: {e}")

    query_file = input_dir / "conceptsQuery.json"
    if query_file.exists():
        with open(query_file, "r", encoding="utf-8") as f: concepts_query = json.load(f)
    return study_text, concepts_query, focus_terms


def build_concepts_query_text(concepts_query: dict) -> str:
    """Serializa los conceptos de conceptsQuery.json a texto plano para la síntesis."""
    parts = []
    for concept in concepts_query.get("concepts", []):
        name = concept.get("name", "").strip()
        query = concept.get("query", "").strip()
        desc = concept.get("description", "").strip()
        part = f"CONCEPT: {name}"
        if query: part += f"\nQUERY: {query}"
        if desc: part += f"\nDESCRIPTION: {desc}"
        parts.append(part)
    return "\n\n".join(parts)


def synthesize_study_context(study_text: str, concepts_query: dict, focus_terms: list[str]) -> str:
    """
    Genera una síntesis en inglés del contexto de estudio (studyDescription +
    conceptsQuery) para usarla como ancla de similitud coseno en el cribado.
    Si el LLM no devuelve nada, concatena y trunca como fallback.
    """
    query_text = build_concepts_query_text(concepts_query)
    combined = "\n\n".join(p for p in (study_text, query_text) if p).strip()

    if not combined:
        writeLog("warning", logger, "[Discovery] No study context available for synthesis.")
        return study_text

    try:
        from sources.common.llm_client import create_resilient_ollama_client
        llm = create_resilient_ollama_client(
            temperature=0.0,
            max_tokens=SYNTHESIS_MAX_TOKENS,
        )
    except Exception as exc:
        writeLog("error", logger,
                 f"[Discovery] LLM client init failed ({exc}). Falling back to concatenation+truncation.")
        return combined[:SYNTHESIS_MAX_CHARS]

    system_prompt = (
        "You are a research project analyst. You will produce a compact English "
        "synthesis of the research focus of a scientific literature review. "
        "The synthesis is used ONLY as a cosine-similarity anchor to filter "
        "relevant papers and concepts, so it must be self-contained, precise, "
        "and keep the specific technical terms of the research queries."
    )
    prompt = (
        "Read the study description and the research concepts below. "
        f"Write a single-paragraph English synthesis between {SYNTHESIS_MIN_TOKENS} and "
        f"{SYNTHESIS_MAX_TOKENS} tokens capturing: the overall goal of the project and each "
        "specific research topic/query, preserving their key technical terms. "
        "Do NOT include meta-commentary, lists, bullet points, or markdown. "
        "Output only the synthesis text.\n\n"
        "=== STUDY DESCRIPTION ===\n"
        f"{study_text[:8000]}\n\n"
        "=== RESEARCH CONCEPTS (conceptsQuery) ===\n"
        f"{query_text[:12000]}"
    )

    try:
        synthesis = llm.generate_text(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=SYNTHESIS_MAX_TOKENS,
            context="discoveryEngine.study_synthesis",
        )
        if synthesis and synthesis.strip():
            writeLog("info", logger,
                     f"[Discovery] Study synthesis generated ({len(synthesis.split())} words)")
            return synthesis.strip()
        writeLog("warning", logger,
                 "[Discovery] LLM returned empty synthesis. Falling back to concatenation+truncation.")
    except Exception as exc:
        writeLog("error", logger,
                 f"[Discovery] LLM synthesis failed ({exc}). Falling back to concatenation+truncation.")

    return combined[:SYNTHESIS_MAX_CHARS]


def extract_study_seeds(study_text: str, concepts_query: dict, focus_terms: list[str]) -> tuple[
    list[str], list[list[str]]]:
    generic_terms = {"natural", "language", "processing", "model", "system", "using", "based", "approach", "method",
                     "data", "learning", "deep", "neural", "paper", "study", "result", "performance", "task", "dataset",
                     "training", "evaluation", "research"}
    study_keywords = []
    for line in study_text.splitlines():
        if line.lower().startswith("keywords:"):
            raw = line.split(":", 1)[1]
            study_keywords = [k.strip().lower() for k in raw.replace("·", ",").split(",") if
                              k.strip() and len(k.strip()) > 2 and k.strip() not in generic_terms]
            break

    seed_topic_list, concept_seed_terms = [], []
    for concept in concepts_query.get("concepts", []):
        raw_terms = concept.get("name", "").lower().split() + concept.get("query", "").lower().split()[
            :20] + concept.get("description", "").lower().split()[:15]
        filtered = [t.strip(".,?():") for t in raw_terms if len(t) > 3 and t not in generic_terms]
        seen, unique = set(), []
        for t in filtered:
            if t not in seen: seen.add(t); unique.append(t)
        top_terms = unique[:12]
        seed_topic_list.append(top_terms)
        concept_seed_terms.extend(top_terms[:6])

    all_seeds = []
    for term in focus_terms:
        if term not in generic_terms: all_seeds.extend([term] * 5)
    for term in study_keywords:
        if term not in generic_terms: all_seeds.extend([term] * 2)
    for term in concept_seed_terms:
        if term not in generic_terms: all_seeds.append(term)

    seen, final_seeds = set(), []
    for t in all_seeds:
        if t not in seen: seen.add(t); final_seeds.append(t)
    return final_seeds[:N_SEED_TERMS], seed_topic_list


# --------------------------------------------------
# HELPERS (Sin cambios)
# --------------------------------------------------
def build_text(paper: dict, seed_terms: Optional[list[str]] = None, max_section_chars: int = 8000) -> str:
    parts = []
    if seed_terms: parts.append(" ".join(seed_terms))
    parts.extend([paper.get("title", ""), paper.get("abstract", ""), " ".join(paper.get("keywords") or [])])
    clean_text = paper.get("clean_text", "")
    if clean_text and len(clean_text) > 500: parts.append(clean_text[:max_section_chars * 2])
    sections = paper.get("clean_sections", {})
    for sec in RELEVANT_SECTIONS:
        sec_text = sections.get(sec, "")
        if sec_text and len(sec_text) > 100: parts.append(sec_text[:max_section_chars])
    if len(" ".join(parts)) < 1000:
        full_text = paper.get("full_text", "")
        if full_text and len(full_text) > 500: parts.append(full_text[:max_section_chars * 2])
    return " ".join(parts)


def collect_candidate_sections(clean_corpus: list[dict], seed_terms: list[str], max_section_chars: int = 8000) -> List[
    Tuple[str, str, str]]:
    candidates = []
    for paper in clean_corpus:
        paper_id = paper.get("paper_id")
        sections = paper.get("clean_sections", {}) or paper.get("sections", {})
        for sec_name in RELEVANT_SECTIONS:
            sec_text = sections.get(sec_name, "")
            if sec_text and len(sec_text) > 50:
                text_parts = []
                if seed_terms: text_parts.append(" ".join(seed_terms))
                text_parts.append(paper.get("title", ""))
                text_parts.append(sec_text[:max_section_chars])
                candidates.append((paper_id, sec_name, " ".join(text_parts)))
    return candidates


def filter_sections_batch(candidates: List[Tuple[str, str, str]], study_embedding: np.ndarray,
                          focus_embedding: np.ndarray, embedding_model: SentenceTransformer,
                          threshold: Optional[float] = None,
                          alpha: Optional[float] = None) -> List[Tuple[str, str, str]]:
    if threshold is None: threshold = SECTION_FILTER_THRESHOLD
    if alpha is None: alpha = SECTION_FILTER_ALPHA
    if not candidates: return []
    texts = [c[2] for c in candidates]
    embeddings = embedding_model.encode(texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False)
    kept = []
    for (paper_id, sec_name, sec_text), emb in zip(candidates, embeddings):
        sim_full = float(np.dot(study_embedding, emb))
        sim = alpha * sim_full + (1 - alpha) * float(
            np.dot(focus_embedding, emb)) if focus_embedding is not None else sim_full
        if sim >= threshold: kept.append((paper_id, sec_name, sec_text))
    return kept


def filter_off_topic_concepts(concepts: list[dict], doc_topic_map: list[dict], embedding_model: SentenceTransformer,
                              study_embedding: np.ndarray, focus_embedding: np.ndarray = None,
                              threshold: Optional[float] = None, alpha: Optional[float] = None,
                              min_topics: Optional[int] = None,
                              min_papers: Optional[int] = None) -> tuple[
    list[dict], list[dict]]:
    """
    Filtra conceptos off-topic. El umbral se ajusta dinámicamente por llamada
    (una por RQ) para garantizar que al menos `min_topics` topics con al menos
    `min_papers` papers sobrevivan, sin subir nunca por encima del umbral base.
    Los valores por defecto se resuelven en tiempo de llamada desde la
    configuración (processControl.defaults["discovery"]).
    """
    if threshold is None: threshold = OFF_TOPIC_THRESHOLD
    if alpha is None: alpha = OFF_TOPIC_ALPHA
    if min_topics is None: min_topics = MIN_TOPICS_PER_RQ
    if min_papers is None: min_papers = MIN_PAPERS_PER_TOPIC
    if study_embedding is None or not concepts: return concepts, doc_topic_map
    scored = []
    for concept in concepts:
        topic_text = " ".join([concept.get("label", ""), " ".join(concept.get("keywords", [])[:10])])
        topic_embedding = embedding_model.encode(topic_text[:2000], normalize_embeddings=True)
        sim_full = float(np.dot(study_embedding, topic_embedding))
        similarity = alpha * sim_full + (1 - alpha) * float(
            np.dot(focus_embedding, topic_embedding)) if focus_embedding is not None else sim_full
        concept["study_similarity"] = round(similarity, 4)
        scored.append(concept)
    scored.sort(key=lambda c: c["study_similarity"], reverse=True)

    valid = [c for c in scored if c.get("n_papers", 0) >= min_papers]
    above_base = [c for c in valid if c["study_similarity"] >= threshold]

    effective = threshold
    if len(above_base) < min_topics:
        if len(valid) >= min_topics:
            effective = min(threshold, valid[min_topics - 1]["study_similarity"])
        elif valid:
            effective = min(threshold, valid[-1]["study_similarity"])
        writeLog("warning", logger,
                 f"[Discovery] Dynamic threshold lowered {threshold:.3f} -> {effective:.3f} "
                 f"to keep at least {min_topics} valid topic(s)")

    kept = [c for c in scored if c["study_similarity"] >= effective]
    removed = [c for c in scored if c["study_similarity"] < effective]
    removed_ids = {c["concept_id"] for c in removed}
    return kept, [entry for entry in doc_topic_map if entry["topic"] not in removed_ids]


# --------------------------------------------------
# NUEVO: MOTOR DE DESCUBRIMIENTO POR SUBCONJUNTO (SLR Orientado a RQ)
# --------------------------------------------------
def discover_topics_for_subset(
        subset_corpus: list[dict],
        rq_seeds: list[str],
        study_embedding: np.ndarray,
        focus_embedding: np.ndarray,
        embedding_model: SentenceTransformer,
        seed_terms: list[str]  # Seeds globales para dar contexto al vectorizador
) -> dict:
    """
    Ejecuta BERTopic o Fallback sobre un subconjunto de papers.
    """
    n = len(subset_corpus)

    if n < 10:
        return fallback_concept_discovery(subset_corpus, seed_terms, study_embedding, focus_embedding, embedding_model,
                                          "")

    candidates = collect_candidate_sections(subset_corpus, seed_terms)
    if len(candidates) < 5:
        return fallback_concept_discovery(subset_corpus, seed_terms, study_embedding, focus_embedding, embedding_model,
                                          "")

    kept_sections = filter_sections_batch(candidates, study_embedding, focus_embedding, embedding_model)
    if len(kept_sections) < 5:
        return fallback_concept_discovery(subset_corpus, seed_terms, study_embedding, focus_embedding, embedding_model,
                                          "")

    chunk_texts = [sec[2] for sec in kept_sections]
    chunk_metadata = [(sec[0], sec[1]) for sec in kept_sections]

    writeLog("info", logger,
             f"  -> Sub-engine processing {len(chunk_texts)} chunks from {len(set(m[0] for m in chunk_metadata))} papers")

    embeddings = embedding_model.encode(chunk_texts, show_progress_bar=False, normalize_embeddings=True,
                                        convert_to_numpy=True, batch_size=32)

    umap_model = UMAP(n_neighbors=max(2, min(15, len(chunk_texts) - 1)), n_components=UMAP_COMPONENTS, metric="cosine",
                      random_state=42)
    hdbscan_model = HDBSCAN(min_cluster_size=max(MIN_CLUSTER_SIZE, min(5, len(chunk_texts) // 15)), min_samples=1,
                            metric="euclidean", cluster_selection_method="eom", prediction_data=True)
    vectorizer = create_custom_vectorizer()

    topic_model = BERTopic(
        umap_model=umap_model, hdbscan_model=hdbscan_model, vectorizer_model=vectorizer,
        seed_topic_list=[rq_seeds] if rq_seeds else None, calculate_probabilities=True, verbose=False
    )
    topics, _ = topic_model.fit_transform(chunk_texts, embeddings)
    topics = list(topics)

    valid_topics = [t for t in topics if t != -1]
    if len(set(valid_topics)) <= 1 or len(valid_topics) < max(2, len(chunk_texts) * 0.15):
        topic_model = BERTopic(
            umap_model=UMAP(n_neighbors=3, n_components=5, random_state=42),
            hdbscan_model=HDBSCAN(min_cluster_size=2, min_samples=1),
            vectorizer_model=vectorizer, seed_topic_list=[rq_seeds] if rq_seeds else None, min_topic_size=2,
            verbose=False
        )
        topics, _ = topic_model.fit_transform(chunk_texts, embeddings)
        topics = list(topics)

    topic_to_chunks = {tid: [] for tid in set(topics) if tid != -1}
    for idx, t in enumerate(topics):
        if t != -1: topic_to_chunks[t].append(idx)

    concepts = []
    for topic_id, chunk_indices in topic_to_chunks.items():
        if len(chunk_indices) < 2: continue
        words = topic_model.get_topic(topic_id)
        filtered_words = [(w, s) for w, s in words[:20] if not re.search(r'\d',
                                                                         w) and w.lower() not in EXTRA_STOPWORDS and w not in DEFAULT_STOPS and len(
            w) > 2]
        if not filtered_words: filtered_words = words[:10]
        label_words = [w for w, _ in filtered_words[:6]]
        paper_ids = list({chunk_metadata[i][0] for i in chunk_indices})
        concepts.append({
            "concept_id": int(topic_id),
            "label": " ".join(label_words)[:80] if label_words else f"topic_{topic_id}",
            "n_chunks": len(chunk_indices), "n_papers": len(paper_ids),
            "keywords": [w for w, _ in filtered_words[:15]], "document_indices": paper_ids,
        })

    doc_topic_map = [{"doc_id": chunk_metadata[idx][0], "topic": t, "section": chunk_metadata[idx][1]} for idx, t in
                     enumerate(topics) if t != -1]
    concepts, doc_topic_map = filter_off_topic_concepts(concepts, doc_topic_map, embedding_model, study_embedding,
                                                        focus_embedding)

    return {
        "n_documents": len(set(e["doc_id"] for e in doc_topic_map)),
        "n_chunks": len(doc_topic_map), "n_topics": len(concepts),
        "concepts": concepts, "doc_topic_map": doc_topic_map,
    }


def fallback_concept_discovery(clean_corpus: list[dict], seed_terms: list[str], study_embedding: np.ndarray,
                               focus_embedding: np.ndarray, embedding_model: SentenceTransformer,
                               study_text: str) -> dict:
    docs = [build_text(p, seed_terms) for p in clean_corpus]
    paper_ids = [p.get("paper_id") for p in clean_corpus]
    n_docs = len(docs)
    dynamic_min_df = 1 if n_docs < 2 else 2
    dynamic_max_df = 1.0 if n_docs < 5 else 0.85
    vectorizer = POSFilterVectorizer(nlp_model=nlp, ngram_range=(1, 2), min_df=dynamic_min_df, max_df=dynamic_max_df,
                                     max_features=2000)
    X = vectorizer.fit_transform(docs)
    n_clus = max(2, min(5, len(docs) // 3))
    labels = KMeans(n_clusters=n_clus, random_state=42).fit_predict(X)
    doc_topic_map = [{"doc_id": paper_ids[idx], "topic": int(label)} for idx, label in enumerate(labels)]
    feature_names = vectorizer.get_feature_names_out()
    concepts = []
    for cluster_id in sorted(set(labels)):
        cluster_docs = [idx for idx, lbl in enumerate(labels) if lbl == cluster_id]
        cluster_center = X[labels == cluster_id].mean(axis=0).A1
        top_indices = cluster_center.argsort()[-10:][::-1]
        top_terms = [feature_names[i] for i in top_indices if
                     not re.search(r'\d', feature_names[i]) and feature_names[i] not in EXTRA_STOPWORDS][:8]
        concepts.append(
            {"concept_id": int(cluster_id), "label": " ".join(top_terms[:4]) if top_terms else f"cluster_{cluster_id}",
             "n_papers": len(cluster_docs), "keywords": top_terms,
             "document_indices": [paper_ids[i] for i in cluster_docs]})
    concepts, doc_topic_map = filter_off_topic_concepts(concepts, doc_topic_map, embedding_model, study_embedding,
                                                        focus_embedding)
    return {"n_documents": len(doc_topic_map), "n_chunks": len(doc_topic_map), "n_topics": len(concepts),
            "concepts": concepts, "doc_topic_map": doc_topic_map}


# --------------------------------------------------
# ENTRY POINT: ORQUESTADOR SLR ORIENTADO A PREGUNTAS
# --------------------------------------------------
def processDiscoveryEngine():
    load_discovery_config()
    input_dir, output_dir = inicioModulo("processDiscoveryEngine")
    text_file = output_dir / "papers_text.json"

    if not text_file.exists():
        writeLog("error", logger, f"[Discovery] Not found: {text_file}")
        return None

    with open(text_file, "r", encoding="utf-8") as f:
        full_corpus = json.load(f)

    study_text, concepts_query, focus_terms = load_study_context(input_dir)
    seed_terms, seed_topic_list = extract_study_seeds(study_text, concepts_query, focus_terms)

    embedding_model = SentenceTransformer(EMBEDDING_MODEL)
    # Ancla de similitud coseno: síntesis de studyDescription + conceptsQuery
    # (antes solo se usaban las objectives del studyDescription)
    synthesis_text = synthesize_study_context(study_text, concepts_query, focus_terms)
    study_embedding = embedding_model.encode(synthesis_text[:4000], normalize_embeddings=True) if synthesis_text else None
    focus_embedding = embedding_model.encode(" ".join(focus_terms), normalize_embeddings=True) if focus_terms else None

    # -----------------------------------------------------------------
    # OPTIMIZACIÓN SOA: Calcular embeddings UNA SOLA VEZ
    # FIX: Usar 'clean_text' (que es donde está el abstract real en este JSON)
    # -----------------------------------------------------------------
    writeLog("info", logger, f"[Discovery] Computing global text embeddings for {len(full_corpus)} papers...")
    full_texts = [p.get("title", "") + " " + p.get("clean_text", "")[:1500] for p in full_corpus]
    global_abstract_embeddings = embedding_model.encode(full_texts, normalize_embeddings=True, batch_size=64,
                                                        show_progress_bar=False)

    rq_list = concepts_query.get("concepts", [])

    if not rq_list:
        writeLog("warning", logger, "[Discovery] No concepts found in conceptsQuery.json. Falling back to global mode.")
        result = discover_topics_for_subset(full_corpus, [], study_embedding, focus_embedding, embedding_model,
                                            seed_terms)
        final_dict_output = {
            "schema_version": "1.5", "n_documents": result["n_documents"], "n_topics": result["n_topics"],
            "concepts": result.get("concepts", []), "doc_topic_map": result.get("doc_topic_map", [])
        }
    else:
        internal_rq_results = []
        writeLog("info", logger,
                 f"[Discovery] Starting Query-Driven Discovery for {len(rq_list)} Research Questions...")

        for i, rq in enumerate(rq_list):
            rq_text_parts = [rq.get("name", ""), rq.get("query", "")]
            rq_query_text = " ".join(rq_text_parts)

            # -----------------------------------------------------
            # FASE 1: HARD FILTER (Filtro Léxico obligatorio)
            # FIX: Buscar en 'clean_text' en lugar de 'abstract' inexistente
            # -----------------------------------------------------
            doc_rq = nlp(rq_query_text)
            rq_key_phrases = [
                token.lemma_.lower() for token in doc_rq
                if token.pos_ in ("NOUN", "PROPN", "ADJ", "VERB")
                   and len(token.text) > 3
                   and not token.is_stop
                   and token.text.lower() not in ("how", "what", "can", "does", "which", "from", "with", "that", "this",
                                                  "than", "both", "are", "been", "have")
            ]
            rq_checklist = set(rq_key_phrases[:15])

            hard_filtered_indices = []
            for idx, paper in enumerate(full_corpus):
                # FIX: Usar title + clean_text
                paper_text = (paper.get("title", "") + " " + paper.get("clean_text", "")).lower()
                matches = sum(1 for keyword in rq_checklist if keyword in paper_text)
                if matches >= 2:
                    hard_filtered_indices.append(idx)

            # -----------------------------------------------------
            # FASE 2: SOFT FILTER (Embeddings)
            # FIX: Controlar la variable 'similarities' correctamente en ambas ramas
            # -----------------------------------------------------
            selected_similarities = np.array([])  # Inicializamos por si falla todo

            if len(hard_filtered_indices) >= 10:
                subset_texts = [full_texts[idx] for idx in hard_filtered_indices]
                subset_embeddings = global_abstract_embeddings[hard_filtered_indices]

                rq_embedding = embedding_model.encode(rq_query_text, normalize_embeddings=True)
                similarities = np.dot(subset_embeddings, rq_embedding)

                k = max(10, int(len(hard_filtered_indices) * 0.8))
                top_local_indices = np.argsort(similarities)[-k:]
                top_indices = [hard_filtered_indices[i] for i in top_local_indices]

                selected_similarities = similarities[top_local_indices]  # Guardamos las sims de los seleccionados
                writeLog("info", logger,
                         f"  -> Hard filter matched {len(hard_filtered_indices)} papers. Soft re-ranked to {len(top_indices)}.")
            else:
                k = int(len(full_corpus) * 0.5)
                rq_embedding = embedding_model.encode(rq_query_text, normalize_embeddings=True)
                similarities = np.dot(global_abstract_embeddings, rq_embedding)

                top_indices = list(np.argsort(similarities)[-k:])
                selected_similarities = similarities[top_indices]  # Guardamos las sims de los seleccionados
                writeLog("info", logger,
                         f"  -> Hard filter too strict ({len(hard_filtered_indices)} papers). Using Top 50% fallback ({k} papers).")

            subset_corpus = [full_corpus[idx] for idx in top_indices]

            # 2. Ejecutar descubrimiento local
            rq_seeds = seed_topic_list[i] if i < len(seed_topic_list) else []
            rq_result = discover_topics_for_subset(
                subset_corpus, rq_seeds, study_embedding, focus_embedding, embedding_model, seed_terms
            )

            # 3. Guardar resultado interno de esta RQ
            # FIX: Usar 'selected_similarities' que siempre existe ahora
            internal_rq_results.append({
                "concept_id": str(i),
                "concept_query": rq.get("query", ""),
                "focus_terms_used": focus_terms,
                "focus_density": float(round(np.mean(selected_similarities), 3)) if len(
                    selected_similarities) > 0 else 0.0,
                "total_evidences": rq_result["n_chunks"],
                "n_clusters": rq_result["n_topics"],
                "clusters": rq_result["concepts"],
                "doc_topic_map": rq_result["doc_topic_map"]
            })

        # -----------------------------------------------------------------
        # COMPATIBILIDAD: Aplanar resultados
        # -----------------------------------------------------------------
        merged_concepts = []
        merged_doc_topic_map = []
        topic_id_offset = 0

        for rq_data in internal_rq_results:
            for concept in rq_data.get("clusters", []):
                concept["concept_id"] = int(concept["concept_id"]) + topic_id_offset
                concept["source_rq"] = rq_data.get("concept_query", "")[:100]
                merged_concepts.append(concept)

            for mapping in rq_data.get("doc_topic_map", []):
                mapping["topic"] = int(mapping["topic"]) + topic_id_offset
                merged_doc_topic_map.append(mapping)

            topic_id_offset += 100

        final_dict_output = {
            "schema_version": "1.5",
            "n_documents": len(set(m["doc_id"] for m in merged_doc_topic_map)),
            "n_topics": len(merged_concepts),
            "concepts": merged_concepts,
            "doc_topic_map": merged_doc_topic_map
        }

    # Guardar outputs
    output_file = output_dir / "candidate_concepts.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(final_dict_output, f, indent=2, ensure_ascii=False)

    output_file_rq = output_dir / "candidate_concepts_by_rq.json"
    if 'internal_rq_results' in locals():
        with open(output_file_rq, "w", encoding="utf-8") as f:
            json.dump(internal_rq_results, f, indent=2, ensure_ascii=False)

    writeLog("info", logger, f"\n{'=' * 60}")
    writeLog("info", logger, f"[Discovery] SLR Query-Driven COMPLETED")
    writeLog("info", logger,
             f"[Discovery] Merged Output: {final_dict_output['n_topics']} unique clusters across all RQs")
    writeLog("info", logger, f"✅ [END] discoveryEngine")

    return final_dict_output


if __name__ == "__main__":
    processDiscoveryEngine()