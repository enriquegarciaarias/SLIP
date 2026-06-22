# concept_evidence.py
from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo

from pathlib import Path
import json
from functools import lru_cache
from collections import defaultdict
import numpy as np
import spacy
from spacy.lang.en import English
from sentence_transformers import SentenceTransformer
from rank_bm25 import BM25Okapi
from sklearn.cluster import AgglomerativeClustering

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"

# Umbrales de similitud para la extracción de ventanas
SIMILARITY_PERCENTILE_CUTOFF = 30      # Retiene el 70% de las ventanas (más laxo)
SIMILARITY_ABSOLUTE_FLOOR = 0.30       # Piso absoluto
MAX_EVIDENCES_PER_CONCEPT = 80         # Límite total de evidencias por concepto

# Parámetros de ventanas deslizantes
WINDOW_SIZE = 5
WINDOW_STEP = 2
MIN_SENTENCES = 3
MIN_WINDOW_WORDS = 20

# Umbral mínimo para que una ventana sea considerada (muy bajo para capturar todos)
MIN_WINDOW_SIMILARITY = 0.05

# Peso del boost por términos de foco (sensores)
FOCUS_TERM_BOOST_WEIGHT = 0.15

# Umbral para clustering intra-documento (más estricto que el global)
INTRADOC_CLUSTER_THRESHOLD = 0.4

# 🔥 NUEVO: Umbral mínimo de similitud para rescatar un documento
# Si la mejor evidencia de un documento no supera este umbral, NO se rescata.
MIN_RESCUE_SIMILARITY = 0.5

# --------------------------------------------------
# MODELS
# --------------------------------------------------

nlp_sent = None
embedding_model = None


def _load_models():
    global nlp_sent, embedding_model
    if nlp_sent is None:
        nlp_sent = English()
        nlp_sent.add_pipe("sentencizer")
    if embedding_model is None:
        embedding_model = SentenceTransformer(EMBEDDING_MODEL)


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def is_noise_window(window_text: str) -> bool:
    lower = window_text.lower()
    bad_patterns = [
        "copyright", "permission", "proceedings", "association for computational linguistics",
        "doi:", "http://", "https://", "www.", "et al.", "fig.", "table", "reference"
    ]
    if any(p in lower for p in bad_patterns) and len(window_text.split()) < 40:
        return True
    if len(window_text.split()) < MIN_WINDOW_WORDS:
        return True
    return False


def is_claim_score(text: str, focus_terms: list[str] = None) -> int:
    generic_patterns = [
        "result", "find", "show", "demonstrate", "evaluate",
        "outperform", "fail", "improve", "achieve", "significantly",
        "conclusion", "propose", "validate", "performance"
    ]
    lower = text.lower()
    score = sum(p in lower for p in generic_patterns)
    if focus_terms:
        focus_patterns = [term.lower() for term in focus_terms if term]
        focus_hits = sum(1 for term in focus_patterns if term in lower)
        score += min(focus_hits, 5)
    return score


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def build_retrieval_query(concept: dict) -> str:
    if concept.get("concept_query"):
        return concept["concept_query"]
    parts = []
    if concept.get("concept_name"):
        parts.append(concept["concept_name"])
    if concept.get("concept_description"):
        parts.append(concept["concept_description"])
    return " ".join(parts)


def split_into_sentences(text: str) -> list[str]:
    if not text:
        return []
    doc = nlp_sent(text)
    return [sent.text.strip() for sent in doc.sents if sent.text.strip()]


def create_sliding_windows(sentences: list[str], window_size: int = WINDOW_SIZE, step: int = WINDOW_STEP) -> list[str]:
    if len(sentences) < window_size:
        return [" ".join(sentences)] if sentences else []
    windows = []
    for start in range(0, len(sentences) - window_size + 1, step):
        window_text = " ".join(sentences[start:start + window_size])
        windows.append(window_text)
    if (len(sentences) - window_size) % step != 0:
        last_window = " ".join(sentences[-window_size:])
        if last_window not in windows:
            windows.append(last_window)
    return windows


def bm25_tokenize(text: str) -> list[str]:
    return text.lower().split()


def score_window(
        window: str,
        query_embeddings: list,
        bm25_model: BM25Okapi,
        tokenized_query: list,
        focus_terms: list[str] = None
) -> float:
    window_embedding = embedding_model.encode(window, normalize_embeddings=True)
    max_sim = max(cosine_similarity(q_emb, window_embedding) for q_emb in query_embeddings)

    tokenized_window = bm25_tokenize(window)
    bm25_score = bm25_model.get_scores(tokenized_query)[0] if bm25_model else 0.0

    claim_boost = is_claim_score(window, focus_terms)

    hybrid = 0.7 * max_sim + 0.3 * bm25_score
    final = hybrid + 0.05 * claim_boost

    if focus_terms:
        lower = window.lower()
        focus_hits = sum(1 for term in focus_terms if term.lower() in lower)
        final += focus_hits * FOCUS_TERM_BOOST_WEIGHT

    return final


# --------------------------------------------------
# QUERY EMBEDDING CACHE
# --------------------------------------------------

@lru_cache(maxsize=128)
def _get_query_embeddings_cached(queries_tuple: tuple) -> list:
    return [
        embedding_model.encode(q, normalize_embeddings=True)
        for q in queries_tuple
    ]


# --------------------------------------------------
# EVIDENCE EXTRACTION (WINDOW LEVEL, SIN LÍMITE POR DOC)
# --------------------------------------------------

def extract_evidences_from_document(
    concept: dict,
    document: dict,
    focus_terms: list[str] = None
) -> list[dict]:
    full_text = document["text"]

    sentences = split_into_sentences(full_text)
    if len(sentences) < MIN_SENTENCES:
        return []

    windows = create_sliding_windows(sentences)
    if not windows:
        return []

    windows = [w for w in windows if not is_noise_window(w)]
    if not windows:
        return []

    primary_query = build_retrieval_query(concept)
    expanded_queries = concept.get("expanded_queries", [])
    queries = [primary_query] + expanded_queries if expanded_queries else [primary_query]
    query_embeddings = _get_query_embeddings_cached(tuple(queries))

    tokenized_corpus = [bm25_tokenize(w) for w in windows]
    bm25 = BM25Okapi(tokenized_corpus)
    tokenized_query = bm25_tokenize(primary_query)
    raw_bm25_scores = np.array(bm25.get_scores(tokenized_query), dtype=float)

    min_bm, max_bm = raw_bm25_scores.min(), raw_bm25_scores.max()
    if max_bm - min_bm > 1e-8:
        norm_bm25 = (raw_bm25_scores - min_bm) / (max_bm - min_bm)
    else:
        norm_bm25 = np.zeros_like(raw_bm25_scores)

    scored_windows = []
    for idx, window_text in enumerate(windows):
        window_emb = embedding_model.encode(window_text, normalize_embeddings=True)
        max_sim = max(cosine_similarity(q_emb, window_emb) for q_emb in query_embeddings)
        bm25_score = norm_bm25[idx]
        final = score_window(
            window_text, query_embeddings, bm25, tokenized_query, focus_terms
        )

        if final >= MIN_WINDOW_SIMILARITY:
            scored_windows.append({
                "text": window_text,
                "score": final,
                "embedding_score": max_sim,
                "bm25_score": bm25_score,
                "claim_score": is_claim_score(window_text, focus_terms),
            })

    evidences = []
    for win_data in scored_windows:
        evidences.append({
            "doc_id": document["doc_id"],
            "paper_title": document["title"],
            "evidence_text": win_data["text"],
            "similarity": round(win_data["score"], 4),
        })
    return evidences


# --------------------------------------------------
# THRESHOLD: percentile-based per concept (con piso absoluto)
# --------------------------------------------------

def filter_by_percentile_threshold(evidences: list[dict]) -> list[dict]:
    if not evidences:
        return []

    above_floor = [e for e in evidences if e["similarity"] >= SIMILARITY_ABSOLUTE_FLOOR]
    if not above_floor:
        return []

    scores = np.array([e["similarity"] for e in above_floor])
    threshold = float(np.percentile(scores, SIMILARITY_PERCENTILE_CUTOFF))
    return [e for e in above_floor if e["similarity"] >= threshold]


# --------------------------------------------------
# CLUSTERIZACIÓN INTRA-DOCUMENTO
# --------------------------------------------------

def cluster_document_evidences(
    evidences: list[dict],
    model: SentenceTransformer,
    distance_threshold: float = INTRADOC_CLUSTER_THRESHOLD
) -> list[dict]:
    """
    Clusteriza las evidencias de un mismo documento y devuelve la mejor de cada cluster.
    """
    if len(evidences) <= 1:
        return evidences

    texts = [ev["evidence_text"] for ev in evidences]
    embeddings = model.encode(texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False)

    clustering = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=distance_threshold,
        metric='cosine',
        linkage='average'
    )
    labels = clustering.fit_predict(embeddings)

    clusters = defaultdict(list)
    for idx, label in enumerate(labels):
        clusters[label].append((evidences[idx], evidences[idx]["similarity"]))

    best_evidences = []
    for cluster_items in clusters.values():
        best = max(cluster_items, key=lambda x: x[1])
        best_evidences.append(best[0])

    return best_evidences


# --------------------------------------------------
# CONCEPT PROCESSING (con clusterización intra-documento y rescate controlado)
# --------------------------------------------------

def process_concept(concept: dict, focus_terms: list[str] = None) -> dict:
    """
    Procesa un concepto:
    1. Extrae ventanas de todos sus documentos.
    2. Elimina duplicados.
    3. Clusteriza intra-documento para reducir redundancia.
    4. Aplica filtro de percentil global.
    5. Rescata al menos una evidencia por documento (solo si supera MIN_RESCUE_SIMILARITY).
    6. Limita a MAX_EVIDENCES_PER_CONCEPT.
    """
    all_evidences = []

    for document in concept["documents"]:
        doc_evidences = extract_evidences_from_document(concept, document, focus_terms)
        all_evidences.extend(doc_evidences)

    # Eliminar duplicados (mismo doc_id y texto exacto)
    unique = []
    seen = set()
    for ev in all_evidences:
        key = (ev["doc_id"], ev["evidence_text"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)

    # Ordenar globalmente por similitud (base)
    unique.sort(key=lambda x: x["similarity"], reverse=True)

    # 🔥 PASO 1: Clusterizar intra-documento
    # Agrupar por doc_id
    docs_evidences = defaultdict(list)
    for ev in unique:
        docs_evidences[ev["doc_id"]].append(ev)

    # Aplicar clustering a cada documento
    reduced_evidences = []
    for doc_id, evs in docs_evidences.items():
        if len(evs) > 1:
            clustered = cluster_document_evidences(evs, embedding_model)
            reduced_evidences.extend(clustered)
            if len(evs) > len(clustered):
                writeLog("debug", logger,
                         f"[Evidence] Doc {doc_id}: {len(evs)} → {len(clustered)} after intra-doc clustering")
        else:
            reduced_evidences.extend(evs)

    # Re-ordenar globalmente después de la reducción
    reduced_evidences.sort(key=lambda x: x["similarity"], reverse=True)

    # 🔥 PASO 2: Aplicar filtro de percentil global
    filtered = filter_by_percentile_threshold(reduced_evidences)

    # 🔥 PASO 3: Rescatar al menos una evidencia por documento (con umbral mínimo)
    represented_docs = set(ev["doc_id"] for ev in filtered)
    for doc_id, evs in docs_evidences.items():
        if doc_id not in represented_docs and evs:
            best_for_doc = evs[0]  # mejor evidencia (ya ordenada)
            # Solo rescatar si supera el umbral mínimo
            if best_for_doc["similarity"] >= MIN_RESCUE_SIMILARITY:
                filtered.append(best_for_doc)
                represented_docs.add(doc_id)
                writeLog("debug", logger,
                         f"[Evidence] Rescued best evidence for doc {doc_id} (sim={best_for_doc['similarity']:.3f})")
            else:
                writeLog("debug", logger,
                         f"[Evidence] Doc {doc_id} NOT rescued (sim={best_for_doc['similarity']:.3f} < {MIN_RESCUE_SIMILARITY})")

    # Re-ordenar final
    filtered.sort(key=lambda x: x["similarity"], reverse=True)

    # Limitar a MAX_EVIDENCES_PER_CONCEPT
    final_evidences = filtered[:MAX_EVIDENCES_PER_CONCEPT]

    return {
        "concept_id": concept["concept_id"],
        "concept_query": concept["concept_query"],
        "knowledge_units": final_evidences
    }


def build_evidence_layer(aligned_concepts: dict, focus_terms: list[str] = None) -> dict:
    concepts_output = []
    for concept in aligned_concepts["aligned_concepts"]:
        writeLog("info", logger, f"[Evidence] Processing concept {concept['concept_id']}")
        concepts_output.append(process_concept(concept, focus_terms))

    return {
        "schema_version": "1.6",   # 🔥 Versión con rescate controlado por umbral
        "embedding_model": EMBEDDING_MODEL,
        "focus_terms_used": focus_terms or [],
        "concepts": concepts_output
    }


def save_output(result: dict, output_file: Path) -> None:
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)


def load_focus_terms(input_dir: Path) -> list[str]:
    json_file = input_dir / "studyDescription.json"
    if json_file.exists():
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("project", {}).get("focus_terms", [])
        except Exception as e:
            writeLog("error", logger, f"[Evidence] Error loading focus_terms: {e}")
    return []


# --------------------------------------------------
# ENTRY POINT
# --------------------------------------------------

def processConceptEvidence():
    _load_models()
    input_dir, output_dir = inicioModulo("processConceptEvidence")
    input_file = output_dir / "aligned_concepts.json"

    if not input_file.exists():
        writeLog("error", logger, f"[Evidence] {input_file} not found")
        return None

    with open(input_file, "r", encoding="utf-8") as f:
        aligned_concepts = json.load(f)

    focus_terms = load_focus_terms(input_dir)
    if focus_terms:
        writeLog("info", logger, f"[Evidence] Loaded {len(focus_terms)} focus terms for boosting")
    else:
        writeLog("info", logger, "[Evidence] No focus terms found – using generic claim boost only")

    writeLog("info", logger,
             "[Evidence] Extracting evidence using sliding windows with intra-document clustering")

    # Expansión de queries (si está disponible)
    expansion_config = {
        "llm_backend": "ollama",
        "llm_model": "qwen3:8b",   # era "llama3.2:3b"
        "llm_url": "http://localhost:11434/api/generate",
        "max_expansion_terms": 7,
        "replace_query": False,
    }

    try:
        from sources.queryExpander import expand_all_concepts
        aligned_concepts = expand_all_concepts(aligned_concepts, expansion_config)
        writeLog("info", logger, "[Evidence] Query expansion completed")
    except Exception as e:
        writeLog("warning", logger, f"[Evidence] Query expansion failed: {e}")
        writeLog("info", logger, "[Evidence] Continuing without expansion")

    result = build_evidence_layer(aligned_concepts, focus_terms)

    output_file = output_dir / "concept_evidence.json"
    save_output(result, output_file)

    writeLog("info", logger, f"[Evidence] Saved to {output_file}")

    # Resumen detallado en consola
    writeLog("info", logger, "\n" + "=" * 80)
    writeLog("info", logger, "📋 RESUMEN DE CONCEPTOS CON EVIDENCIAS (knowledge_units)")
    writeLog("info", logger, "=" * 80)

    total_units = 0
    for concept_out in result.get("concepts", []):
        concept_id = concept_out.get("concept_id")
        concept_query = concept_out.get("concept_query", "")
        knowledge_units = concept_out.get("knowledge_units", [])
        n_units = len(knowledge_units)
        total_units += n_units

        doc_ids = sorted(set(unit.get("doc_id") for unit in knowledge_units if unit.get("doc_id")))
        doc_ids_str = ", ".join(doc_ids) if doc_ids else "ninguno"

        writeLog("info", logger, f"\n🔹 Concepto {concept_id}:")
        if len(concept_query) > 80:
            query_short = concept_query[:80] + "..."
        else:
            query_short = concept_query
        writeLog("info", logger, f"   Pregunta: {query_short}")
        writeLog("info", logger, f"   📄 Knowledge units: {n_units}")
        writeLog("info", logger, f"   🆔 Documentos únicos: {doc_ids_str}")

    writeLog("info", logger, "\n" + "-" * 80)
    writeLog("info", logger, f"✅ Total de knowledge units (evidencias): {total_units}")
    writeLog("info", logger, "=" * 80)

    return result


if __name__ == "__main__":
    processConceptEvidence()