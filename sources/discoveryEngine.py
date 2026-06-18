# discoveryEngine.py
from sources.common.common import logger, processControl, writeLog

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
except OSError:
    import subprocess
    writeLog("info", logger, "[Discovery] spaCy model not found. Downloading en_core_web_sm...")
    subprocess.run(["python", "-m", "spacy", "download", "en_core_web_sm"])
    import spacy
    nlp = spacy.load("en_core_web_sm", disable=["ner", "parser"])
except ImportError:
    writeLog("error", logger, "[Discovery] spaCy not installed. Please `pip install spacy` and download model.")
    raise

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"

# Minimum cosine similarity between a discovered topic and the
# studyDescription embedding for the topic to be kept.
OFF_TOPIC_THRESHOLD = 0.30

# How many seed terms from studyDescription to prepend to each paper.
N_SEED_TERMS = 30

# Sections to consider (ordered by importance)
RELEVANT_SECTIONS = ["abstract", "introduction", "methodology", "results", "conclusion"]

# UMAP / HDBSCAN
UMAP_COMPONENTS = 8
MIN_CLUSTER_SIZE = 3

# Extra stopwords (including numbers, months, academic boilerplate)
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
# CUSTOM VECTORIZER WITH SPACY + POS FILTERING + N-GRAMS
# --------------------------------------------------

class POSFilterVectorizer(CountVectorizer):
    """
    Tokenizes with spaCy, keeps only NOUN, ADJ, PROPN, lemmatizes,
    removes stopwords, numbers, short tokens. Generates unigrams + bigrams.
    """
    def __init__(self, nlp_model, ngram_range=(1, 2), min_df=2, max_df=0.85, max_features=5000, **kwargs):
        super().__init__(
            ngram_range=ngram_range,
            min_df=min_df,
            max_df=max_df,
            max_features=max_features,
            **kwargs
        )
        self.nlp_model = nlp_model

    def build_tokenizer(self):
        def tokenize(text):
            doc = self.nlp_model(text)
            tokens = []
            for token in doc:
                if token.pos_ in ("NOUN", "ADJ", "PROPN"):
                    if (not token.is_stop and
                        not token.like_num and
                        len(token.text) > 2 and
                        not any(c.isdigit() for c in token.text)):
                        lemma = token.lemma_.lower()
                        if lemma not in EXTRA_STOPWORDS and len(lemma) > 2:
                            tokens.append(lemma)
            return tokens
        return tokenize


def create_custom_vectorizer() -> CountVectorizer:
    return POSFilterVectorizer(
        nlp_model=nlp,
        ngram_range=(1, 2),
        min_df=2,
        max_df=0.85,
        max_features=5000
    )


# --------------------------------------------------
# ORIENTATION — read study context from JSON
# --------------------------------------------------

def load_study_context(input_dir: Path) -> tuple[str, dict, list[str]]:
    """
    Lee studyDescription.json del directorio input_dir.
    Devuelve (study_text, concepts_query, focus_terms).
    """
    json_file = input_dir / "studyDescription.json"
    desc_file = input_dir / "studyDescription.txt"   # compatibilidad con versión anterior

    study_text = ""
    focus_terms = []
    concepts_query = {}

    # Intentar JSON primero
    if json_file.exists():
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            project = data.get("project", {})
            objectives = project.get("objectives", [])
            study_text = " ".join(objectives) if objectives else ""
            keywords = project.get("keywords", [])
            focus_terms = project.get("focus_terms", [])
            # concepts_query se cargará después (es un archivo separado)
            writeLog("info", logger, f"[Discovery] Loaded studyDescription.json: {len(objectives)} objectives, {len(focus_terms)} focus terms")
        except Exception as e:
            writeLog("error", logger, f"[Discovery] Error reading studyDescription.json: {e}")

    # Si no hay JSON, intentar TXT (legacy)
    elif desc_file.exists():
        content = desc_file.read_text(encoding="utf-8").strip()
        study_text = content
        # Extraer Keywords: y FocusTerms: de forma rudimentaria (para compatibilidad)
        kw_match = re.search(r'^Keywords:\s*(.+?)(?=\n\s*\n|\Z)', content, re.MULTILINE | re.DOTALL)
        if kw_match:
            raw = kw_match.group(1)
            # No hacemos nada con keywords ahora, pero podríamos
            pass
        focus_match = re.search(r'^FocusTerms:\s*(.+?)(?=\n\s*\n|\Z)', content, re.MULTILINE | re.DOTALL)
        if focus_match:
            raw = focus_match.group(1)
            focus_terms = [
                t.strip().lower()
                for t in re.split(r'[·,;]', raw)
                if t.strip() and len(t.strip()) > 2
            ]
        writeLog("info", logger, f"[Discovery] Loaded studyDescription.txt (legacy)")

    else:
        writeLog("info", logger,
                 f"[Discovery] No studyDescription.json or .txt found in {input_dir} — running without domain orientation")

    # Cargar conceptsQuery.json (si existe)
    query_file = input_dir / "conceptsQuery.json"
    if query_file.exists():
        with open(query_file, "r", encoding="utf-8") as f:
            concepts_query = json.load(f)
    else:
        writeLog("info", logger,
                 f"[Discovery] conceptsQuery.json not found — "
                 f"seed_topic_list will be empty")

    return study_text, concepts_query, focus_terms


# --------------------------------------------------
# SEED TERMS + SEED TOPIC LIST
# --------------------------------------------------

def extract_study_seeds(
    study_text:     str,
    concepts_query: dict,
    focus_terms:    list[str],
) -> tuple[list[str], list[list[str]]]:
    """
    Construye seed_terms (con peso a focus_terms) y seed_topic_list
    a partir de concepts_query.
    """
    generic_terms = {
        "natural", "language", "processing", "model", "system",
        "using", "based", "approach", "method", "data", "learning",
        "deep", "neural", "paper", "study", "result", "performance",
        "task", "dataset", "training", "evaluation", "research",
    }

    # 1. Extraer keywords del estudio (solo si study_text contiene "Keywords:")
    study_keywords = []
    for line in study_text.splitlines():
        if line.lower().startswith("keywords:"):
            raw = line.split(":", 1)[1]
            study_keywords = [
                k.strip().lower()
                for k in raw.replace("·", ",").split(",")
                if k.strip() and len(k.strip()) > 2 and k.strip() not in generic_terms
            ]
            break
    # Si no se encontró, intentar extraer del JSON? Pero study_text ya es el texto completo.

    # 2. Construir seed_topic_list a partir de concepts_query
    seed_topic_list = []
    concept_seed_terms = []

    for concept in concepts_query.get("concepts", []):
        name = concept.get("name", "")
        query = concept.get("query", "")
        desc = concept.get("description", "")
        raw_terms = []
        raw_terms.extend(name.lower().replace("-", " ").split())
        raw_terms.extend(query.lower().split()[:20])
        raw_terms.extend(desc.lower().split()[:15])
        filtered = [
            t.strip(".,?():")
            for t in raw_terms
            if len(t) > 3 and t not in generic_terms
        ]
        seen = set()
        unique = []
        for t in filtered:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        top_terms = unique[:12]
        seed_topic_list.append(top_terms)
        concept_seed_terms.extend(top_terms[:6])

    # 3. Combinar todos los términos con pesos (focus_terms con peso 5, study_keywords peso 2, concept_seed_terms peso 1)
    all_seeds = []
    if focus_terms:
        for term in focus_terms:
            if term not in generic_terms:
                all_seeds.extend([term] * 5)
    for term in study_keywords:
        if term not in generic_terms:
            all_seeds.extend([term] * 2)
    for term in concept_seed_terms:
        if term not in generic_terms:
            all_seeds.append(term)

    # Eliminar duplicados manteniendo orden (primera aparición)
    seen = set()
    final_seeds = []
    for t in all_seeds:
        if t not in seen:
            seen.add(t)
            final_seeds.append(t)

    seed_terms = final_seeds[:N_SEED_TERMS]

    writeLog("info", logger,
             f"[Discovery] Seed terms ({len(seed_terms)}): "
             f"{', '.join(seed_terms[:10])}{'...' if len(seed_terms) > 10 else ''}")
    if seed_topic_list:
        writeLog("info", logger,
                 f"[Discovery] Seed topic list: {len(seed_topic_list)} topics")

    return seed_terms, seed_topic_list


# --------------------------------------------------
# TEXT BUILDER with seed prepending
# --------------------------------------------------

def build_text(paper: dict, seed_terms: Optional[list[str]] = None) -> str:
    parts = []
    if seed_terms:
        parts.append(" ".join(seed_terms))
    parts.extend([
        paper.get("title", ""),
        paper.get("abstract", ""),
        " ".join(paper.get("keywords") or []),
    ])
    clean_text = paper.get("clean_text", "")
    if clean_text and len(clean_text) > 500:
        parts.append(clean_text[:8000])
    sections = paper.get("clean_sections", {})
    for sec in RELEVANT_SECTIONS:
        sec_text = sections.get(sec, "")
        if sec_text and len(sec_text) > 100:
            parts.append(sec_text[:4000])
    if len(" ".join(parts)) < 1000:
        full_text = paper.get("full_text", "")
        if full_text and len(full_text) > 500:
            parts.append(full_text[:8000])
    return " ".join(parts)


# --------------------------------------------------
# SECTION COLLECTION WITH BATCH ENCODING (EFFICIENCY)
# --------------------------------------------------

def collect_candidate_sections(
    clean_corpus: list[dict],
    seed_terms: list[str]
) -> List[Tuple[str, str, str]]:
    """
    Returns a list of (paper_id, section_name, section_text) for all sections
    that have enough text. No filtering by relevance yet – that will be done
    with batch embedding.
    """
    candidates = []
    for paper in clean_corpus:
        paper_id = paper.get("paper_id")
        sections = paper.get("clean_sections", {}) or paper.get("sections", {})
        for sec_name in RELEVANT_SECTIONS:
            sec_text = sections.get(sec_name, "")
            if sec_text and len(sec_text) > 50:
                # Build final text with seed terms and title (same as filter_sections_by_relevance would do)
                text_parts = []
                if seed_terms:
                    text_parts.append(" ".join(seed_terms))
                text_parts.append(paper.get("title", ""))
                text_parts.append(sec_text)
                final_text = " ".join(text_parts)
                candidates.append((paper_id, sec_name, final_text))
    return candidates


def filter_sections_batch(
    candidates: List[Tuple[str, str, str]],
    study_embedding: np.ndarray,
    focus_embedding: np.ndarray,
    embedding_model: SentenceTransformer,
    threshold: float = OFF_TOPIC_THRESHOLD,
    alpha: float = 0.5,
) -> List[Tuple[str, str, str]]:
    """
    Encodes candidate texts and filters by hybrid similarity.
    """
    if not candidates:
        return []
    texts = [c[2] for c in candidates]
    embeddings = embedding_model.encode(
        texts,
        normalize_embeddings=True,
        batch_size=32,
        show_progress_bar=False
    )
    kept = []
    for (paper_id, sec_name, sec_text), emb in zip(candidates, embeddings):
        sim_full = float(np.dot(study_embedding, emb))
        if focus_embedding is not None:
            sim_focus = float(np.dot(focus_embedding, emb))
            sim = alpha * sim_full + (1 - alpha) * sim_focus
        else:
            sim = sim_full
        if sim >= threshold:
            kept.append((paper_id, sec_name, sec_text))
    return kept


# --------------------------------------------------
# POST-CLUSTERING FILTER (reuses embeddings)
# --------------------------------------------------

def filter_off_topic_concepts(
    concepts:        list[dict],
    doc_topic_map:   list[dict],
    embedding_model: SentenceTransformer,
    study_embedding: np.ndarray,
    focus_embedding: np.ndarray = None,
    threshold:       float = OFF_TOPIC_THRESHOLD,
    alpha:           float = 0.5,
) -> tuple[list[dict], list[dict]]:
    """
    Filters out topics that are off-topic with respect to the study.
    Uses hybrid similarity if focus_embedding is provided.
    """
    if study_embedding is None or not concepts:
        return concepts, doc_topic_map

    kept = []
    removed = []

    for concept in concepts:
        topic_text = " ".join([
            concept.get("label", ""),
            " ".join(concept.get("keywords", [])[:10]),
        ])
        topic_embedding = embedding_model.encode(
            topic_text[:2000],
            normalize_embeddings=True,
        )
        sim_full = float(np.dot(study_embedding, topic_embedding))
        if focus_embedding is not None:
            sim_focus = float(np.dot(focus_embedding, topic_embedding))
            similarity = alpha * sim_full + (1 - alpha) * sim_focus
        else:
            similarity = sim_full
        concept["study_similarity"] = round(similarity, 4)

        if similarity >= threshold:
            kept.append(concept)
            writeLog("info", logger,
                     f"[Discovery] ✓ topic {concept['concept_id']} "
                     f"'{concept['label'][:40]}' sim={similarity:.3f} — kept")
        else:
            removed.append(concept)
            writeLog("info", logger,
                     f"[Discovery] ✗ topic {concept['concept_id']} "
                     f"'{concept['label'][:40]}' sim={similarity:.3f} < {threshold} — off-topic, removed")

    if removed:
        writeLog("info", logger, f"[Discovery] Filtered {len(removed)} off-topic topic(s)")

    removed_ids = {c["concept_id"] for c in removed}
    filtered_map = [entry for entry in doc_topic_map if entry["topic"] not in removed_ids]

    return kept, filtered_map


# --------------------------------------------------
# BERTOPIC ENGINE (chunk-level, no majority vote)
# --------------------------------------------------

def concept_discovery_bertopic(
    clean_corpus:    list[dict],
    seed_terms:      list[str],
    seed_topic_list: list[list[str]],
    study_embedding: np.ndarray,
    focus_embedding: np.ndarray,
    embedding_model: SentenceTransformer,   # 🔥 NUEVO: recibir modelo preinstanciado
    study_text:      str,
) -> dict:

    # 1. Collect all candidate sections and filter by relevance (batch)
    candidates = collect_candidate_sections(clean_corpus, seed_terms)
    if len(candidates) < 5:
        writeLog("warning", logger, "[BERTopic] Too few sections after candidate collection, falling back")
        return fallback_concept_discovery(clean_corpus, seed_terms, study_embedding, focus_embedding, embedding_model, study_text)

    kept_sections = filter_sections_batch(
        candidates,
        study_embedding,
        focus_embedding,
        embedding_model,
        threshold=OFF_TOPIC_THRESHOLD
    )

    if len(kept_sections) < 5:
        writeLog("warning", logger, "[BERTopic] Too few sections after relevance filtering, falling back")
        return fallback_concept_discovery(clean_corpus, seed_terms, study_embedding, focus_embedding, embedding_model, study_text)

    # Prepare chunk texts and metadata
    chunk_texts = [sec[2] for sec in kept_sections]
    chunk_metadata = [(sec[0], sec[1]) for sec in kept_sections]  # (paper_id, section_name)

    writeLog("info", logger, f"[BERTopic] Working with {len(chunk_texts)} chunks from {len(set(m[0] for m in chunk_metadata))} papers")

    # 2. Encode chunks (usando el modelo ya cargado)
    embeddings = embedding_model.encode(
        chunk_texts,
        show_progress_bar=True,
        normalize_embeddings=True,
        convert_to_numpy=True,
        batch_size=16,
    )

    # 3. UMAP + HDBSCAN
    umap_model = UMAP(
        n_neighbors=max(2, min(15, len(chunk_texts) - 1)),
        n_components=UMAP_COMPONENTS,
        metric="cosine",
        random_state=42,
    )
    hdbscan_model = hdbscan.HDBSCAN(
        min_cluster_size=max(MIN_CLUSTER_SIZE, min(5, len(chunk_texts) // 15)),
        min_samples=1,
        metric="euclidean",
        cluster_selection_method="eom",
        prediction_data=True,
    )

    # 4. BERTopic with guided seed topics and custom vectorizer
    vectorizer = create_custom_vectorizer()
    topic_model = BERTopic(
        umap_model=umap_model,
        hdbscan_model=hdbscan_model,
        vectorizer_model=vectorizer,
        seed_topic_list=seed_topic_list if seed_topic_list else None,
        calculate_probabilities=True,
        verbose=True,
    )

    topics, probs = topic_model.fit_transform(chunk_texts, embeddings)
    topics = list(topics)

    # Fallback if no clusters found
    valid_topics = [t for t in topics if t != -1]
    if len(set(valid_topics)) <= 1 or len(valid_topics) < max(2, len(chunk_texts) * 0.15):
        writeLog("info", logger, "[BERTopic] Insufficient clusters → fallback params")
        topic_model = BERTopic(
            umap_model=UMAP(n_neighbors=3, n_components=5, random_state=42),
            hdbscan_model=HDBSCAN(min_cluster_size=2, min_samples=1),
            vectorizer_model=vectorizer,
            seed_topic_list=seed_topic_list if seed_topic_list else None,
            min_topic_size=2,
            verbose=True,
        )
        topics, probs = topic_model.fit_transform(chunk_texts, embeddings)
        topics = list(topics)

    # 5. Extract topic info and build concepts
    topic_info = topic_model.get_topic_info()
    concepts = []
    # For each topic, we need paper_ids and chunks count
    topic_to_chunks = {tid: [] for tid in set(topics) if tid != -1}
    for idx, t in enumerate(topics):
        if t != -1:
            topic_to_chunks[t].append(idx)

    for topic_id, chunk_indices in topic_to_chunks.items():
        if len(chunk_indices) < 2:
            continue
        words = topic_model.get_topic(topic_id)
        # Filter keywords with regex (no digits) and extra stopwords
        filtered_words = []
        for w, s in words[:20]:
            if re.search(r'\d', w):
                continue
            if w.lower() in EXTRA_STOPWORDS or w in DEFAULT_STOPS:
                continue
            if len(w) < 3:
                continue
            filtered_words.append((w, s))
        if not filtered_words:
            filtered_words = words[:10]
        label_words = [w for w, _ in filtered_words[:6]]
        label = " ".join(label_words)[:80]

        # Unique paper_ids for this topic
        paper_ids = list({chunk_metadata[i][0] for i in chunk_indices})
        concepts.append({
            "concept_id": int(topic_id),
            "label": label if label else f"topic_{topic_id}",
            "n_chunks": len(chunk_indices),
            "n_papers": len(paper_ids),
            "keywords": [w for w, _ in filtered_words[:15]],
            "document_indices": paper_ids,
        })

    # 6. Build doc_topic_map at chunk level (NO majority vote)
    # Each entry: {"doc_id": paper_id, "topic": topic_id, "section": section_name}
    doc_topic_map = []
    for idx, t in enumerate(topics):
        if t == -1:
            continue
        paper_id, section_name = chunk_metadata[idx]
        doc_topic_map.append({
            "doc_id": paper_id,
            "topic": t,
            "section": section_name
        })

    # 7. Filter off-topic concepts (reuses study_embedding and focus_embedding)
    concepts, doc_topic_map = filter_off_topic_concepts(
        concepts,
        doc_topic_map,
        embedding_model,
        study_embedding,
        focus_embedding,
        OFF_TOPIC_THRESHOLD
    )

    # Remove concepts that lost all chunks (should not happen)
    valid_topic_ids = {c["concept_id"] for c in concepts}
    doc_topic_map = [e for e in doc_topic_map if e["topic"] in valid_topic_ids]

    return {
        "schema_version": "1.4",
        "n_documents": len(set(e["doc_id"] for e in doc_topic_map)),  # unique papers
        "n_chunks": len(doc_topic_map),                               # total assignments
        "n_topics": len(concepts),
        "concepts": concepts,
        "doc_topic_map": doc_topic_map,
    }


# --------------------------------------------------
# FALLBACK ENGINE (simplified, but using same POS vectorizer)
# --------------------------------------------------

def fallback_concept_discovery(
    clean_corpus:    list[dict],
    seed_terms:      list[str],
    study_embedding: np.ndarray,
    focus_embedding: np.ndarray,
    embedding_model: SentenceTransformer,   # 🔥 NUEVO
    study_text:      str,
) -> dict:
    # Build full-text documents (no chunking)
    docs = [build_text(p, seed_terms) for p in clean_corpus]
    paper_ids = [p.get("paper_id") for p in clean_corpus]

    # Use POSFilterVectorizer for TF-IDF
    vectorizer = POSFilterVectorizer(
        nlp_model=nlp,
        ngram_range=(1, 2),
        min_df=2,
        max_df=0.85,
        max_features=2000
    )
    X = vectorizer.fit_transform(docs)
    n_clus = max(2, min(5, len(docs) // 3))
    labels = KMeans(n_clusters=n_clus, random_state=42).fit_predict(X)

    doc_topic_map = [
        {"doc_id": paper_ids[idx], "topic": int(label)}
        for idx, label in enumerate(labels)
    ]

    feature_names = vectorizer.get_feature_names_out()
    concepts = []
    for cluster_id in sorted(set(labels)):
        cluster_docs = [idx for idx, lbl in enumerate(labels) if lbl == cluster_id]
        cluster_center = X[labels == cluster_id].mean(axis=0).A1
        top_indices = cluster_center.argsort()[-10:][::-1]
        top_terms = [feature_names[i] for i in top_indices
                     if not re.search(r'\d', feature_names[i]) and feature_names[i] not in EXTRA_STOPWORDS][:8]
        concepts.append({
            "concept_id": int(cluster_id),
            "label": " ".join(top_terms[:4]) if top_terms else f"cluster_{cluster_id}",
            "n_papers": len(cluster_docs),
            "keywords": top_terms,
            "document_indices": [paper_ids[i] for i in cluster_docs],
        })

    concepts, doc_topic_map = filter_off_topic_concepts(
        concepts,
        doc_topic_map,
        embedding_model,
        study_embedding,
        focus_embedding,
        OFF_TOPIC_THRESHOLD
    )

    return {
        "schema_version": "1.4",
        "n_documents": len(doc_topic_map),
        "n_chunks": len(doc_topic_map),  # in fallback, same as n_documents
        "n_topics": len(concepts),
        "concepts": concepts,
        "doc_topic_map": doc_topic_map,
    }


# --------------------------------------------------
# ENTRY POINT
# --------------------------------------------------

def processDiscoveryEngine():
    writeLog("info", logger, "🚀 [START] processDiscoveryEngine (chunk-level, POS filtering, multi-topic papers)")

    base_input_dir = Path(processControl.env.get("input", ""))
    base_output_dir = Path(processControl.env.get("output", ""))
    subject = processControl.args.subject

    output_dir = base_output_dir / subject
    input_dir = base_input_dir / subject
    text_file = output_dir / "papers_text.json"

    if not text_file.exists():
        writeLog("error", logger, f"[Discovery] Not found: {text_file}")
        return None

    with open(text_file, "r", encoding="utf-8") as f:
        corpus = json.load(f)

    writeLog("info", logger, f"[Discovery] Loaded {len(corpus)} papers from papers_text.json")

    # 🔥 NUEVO: leer contexto (ahora JSON)
    study_text, concepts_query, focus_terms = load_study_context(input_dir)
    if study_text:
        writeLog("info", logger, f"[Discovery] Study description loaded ({len(study_text)} chars)")
    if concepts_query:
        writeLog("info", logger, f"[Discovery] {len(concepts_query.get('concepts', []))} objective concepts loaded")
    if focus_terms:
        writeLog("info", logger, f"[Discovery] Focus terms: {', '.join(focus_terms[:10])}...")

    # Extraer seeds (ahora devuelve seed_terms y seed_topic_list)
    seed_terms, seed_topic_list = extract_study_seeds(study_text, concepts_query, focus_terms)

    # 🔥 NUEVO: instanciar modelo UNA SOLA VEZ
    embedding_model = SentenceTransformer(EMBEDDING_MODEL)

    # Calcular study_embedding (del texto completo)
    study_embedding = embedding_model.encode(
        study_text[:4000] if study_text else "",
        normalize_embeddings=True,
    ) if study_text else None

    # 🔥 NUEVO: focus_embedding SOLO con focus_terms (sin texto adicional)
    focus_embedding = None
    if focus_terms:
        focus_text = " ".join(focus_terms)
        focus_embedding = embedding_model.encode(
            focus_text,
            normalize_embeddings=True,
        )
        writeLog("info", logger, f"[Discovery] Focus embedding computed from {len(focus_terms)} terms")

    # Ejecutar discovery pasando el modelo
    n = len(corpus)
    if n < 10:
        result = fallback_concept_discovery(
            corpus,
            seed_terms,
            study_embedding,
            focus_embedding,
            embedding_model,   # 🔥 NUEVO
            study_text
        )
    else:
        if n < 20:
            writeLog("info", logger, "[BERTopic] Small corpus (<20 docs) — results may be unstable")
        result = concept_discovery_bertopic(
            corpus,
            seed_terms,
            seed_topic_list,
            study_embedding,
            focus_embedding,
            embedding_model,   # 🔥 NUEVO
            study_text
        )

    # Guardar output
    output_file = output_dir / "candidate_concepts.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    writeLog("info", logger, f"[Discovery] Saved to {output_file}")
    writeLog("info", logger,
             f"[Discovery] {result['n_topics']} topics, "
             f"{result['n_documents']} papers, {result.get('n_chunks', result['n_documents'])} chunk assignments")
    writeLog("info", logger, "✅ [END] discoveryEngine")

    return result


if __name__ == "__main__":
    processDiscoveryEngine()