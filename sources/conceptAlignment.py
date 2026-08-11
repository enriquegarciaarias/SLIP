# concept_alignment.py
from sources.common.common import logger, processControl, writeLog
from sources.section_indexer import ( build_enhanced_concept_text, determine_concept_type, SECTION_WEIGHTS )
from sources.common.utils import inicioModulo

from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
import json
from pathlib import Path

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

EMBEDDING_MODEL          = "BAAI/bge-large-en-v1.5"
TOP_K_ASSIGNMENTS        = 3
ALIGNMENT_SCORE_THRESHOLD = 0.40
TOPIC_DOC_TEXT_LIMIT     = 4000
TOPIC_DOCS_FOR_EMBEDDING = 10
USE_ENHANCED_EMBEDDINGS  = True

# --- Configuración de Re-ranking y Selección Dinámica ---
RERANK_PERCENTILE = 60
MIN_DOCS_AFTER_RERANK = 5

# Peso para combinar scores de alineación y re-ranking
ALIGNMENT_SCORE_WEIGHT = 0.3
RERANK_SCORE_WEIGHT = 0.7

# --- Límites dinámicos de documentos por concepto ---
# Ya NO usamos un corte duro de 30. Usamos calidad.
MIN_DOCS_PER_CONCEPT = 5       # Mínimo absoluto para poder analizar un concepto
MAX_DOCS_PER_CONCEPT = 60      # Tope de seguridad para no saturar el siguiente paso
MIN_RERANK_SCORE = 0.45         # Umbral de calidad mínimo para aceptar un documento


def load_concept_alignment_config() -> None:
    """
    Sobrescribe las constantes del módulo con la sección "conceptAlignment"
    de config.json (processControl.defaults). Los valores del fichero tienen
    prioridad; si faltan, se conservan los valores por defecto.
    """
    defaults = getattr(processControl, "defaults", None) or {}
    cfg = defaults.get("conceptAlignment", {}) if isinstance(defaults, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}

    global EMBEDDING_MODEL, TOP_K_ASSIGNMENTS, ALIGNMENT_SCORE_THRESHOLD, \
        TOPIC_DOC_TEXT_LIMIT, TOPIC_DOCS_FOR_EMBEDDING, USE_ENHANCED_EMBEDDINGS, \
        RERANK_PERCENTILE, MIN_DOCS_AFTER_RERANK, ALIGNMENT_SCORE_WEIGHT, \
        RERANK_SCORE_WEIGHT, MIN_DOCS_PER_CONCEPT, MAX_DOCS_PER_CONCEPT, MIN_RERANK_SCORE

    EMBEDDING_MODEL = cfg.get("embedding_model", EMBEDDING_MODEL)
    TOP_K_ASSIGNMENTS = int(cfg.get("top_k_assignments", TOP_K_ASSIGNMENTS))
    ALIGNMENT_SCORE_THRESHOLD = float(cfg.get("alignment_score_threshold", ALIGNMENT_SCORE_THRESHOLD))
    TOPIC_DOC_TEXT_LIMIT = int(cfg.get("topic_doc_text_limit", TOPIC_DOC_TEXT_LIMIT))
    TOPIC_DOCS_FOR_EMBEDDING = int(cfg.get("topic_docs_for_embedding", TOPIC_DOCS_FOR_EMBEDDING))
    USE_ENHANCED_EMBEDDINGS = bool(cfg.get("use_enhanced_embeddings", USE_ENHANCED_EMBEDDINGS))
    RERANK_PERCENTILE = int(cfg.get("rerank_percentile", RERANK_PERCENTILE))
    MIN_DOCS_AFTER_RERANK = int(cfg.get("min_docs_after_rerank", MIN_DOCS_AFTER_RERANK))
    ALIGNMENT_SCORE_WEIGHT = float(cfg.get("alignment_score_weight", ALIGNMENT_SCORE_WEIGHT))
    RERANK_SCORE_WEIGHT = float(cfg.get("rerank_score_weight", RERANK_SCORE_WEIGHT))
    MIN_DOCS_PER_CONCEPT = int(cfg.get("min_docs_per_concept", MIN_DOCS_PER_CONCEPT))
    MAX_DOCS_PER_CONCEPT = int(cfg.get("max_docs_per_concept", MAX_DOCS_PER_CONCEPT))
    MIN_RERANK_SCORE = float(cfg.get("min_rerank_score", MIN_RERANK_SCORE))


# --------------------------------------------------
# FUNCIONES AUXILIARES
# --------------------------------------------------

def load_focus_terms(input_dir: Path) -> list[str]:
    json_file = input_dir / "studyDescription.json"
    focus_terms = []
    if json_file.exists():
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            project = data.get("project", {})
            focus_terms = project.get("focus_terms", [])
            if focus_terms:
                writeLog("info", logger, f"[ConceptAlignment] Loaded {len(focus_terms)} focus terms")
            else:
                writeLog("info", logger, "[ConceptAlignment] No focus_terms found in studyDescription.json")
        except Exception as e:
            writeLog("error", logger, f"[ConceptAlignment] Error reading studyDescription.json: {e}")
    else:
        writeLog("info", logger, "[ConceptAlignment] studyDescription.json not found — running without focus terms")
    return focus_terms


def build_concept_text(concept: dict) -> str:
    parts = []
    label = concept.get("topic_label", "").strip()
    if label:
        parts.append(label)
    seen_kw = set()
    for kw in concept.get("keywords", []):
        kw_clean = kw.strip()
        if kw_clean and kw_clean not in seen_kw:
            parts.append(kw_clean)
            seen_kw.add(kw_clean)
    docs = concept.get("documents", [])
    substantive_added = 0
    for doc in docs:
        if substantive_added >= TOPIC_DOCS_FOR_EMBEDDING:
            break
        text = doc.get("text", "").strip()
        if len(text) < 300:
            title = doc.get("title", "").strip()
            if title:
                parts.append(title)
        else:
            parts.append(text[:TOPIC_DOC_TEXT_LIMIT])
            substantive_added += 1
    return " ".join(parts)


def build_objective_text(concept: dict, focus_terms: list[str] = None) -> str:
    """
    Construye el texto de embedding para el objetivo de investigación.
    Se duplican los focus_terms para darles más peso semántico.
    """
    parts = [
        concept.get("name", ""),
        concept.get("query", ""),
        concept.get("description", ""),
    ]
    if focus_terms:
        focus_text = " ".join(focus_terms)
        parts.append(focus_text)
        parts.append(focus_text)
    return " ".join(parts)


# --------------------------------------------------
# I/O
# --------------------------------------------------

def load_corpus(output_dir) -> list:
    text_file = output_dir / "papers_text.json"
    if not text_file.exists():
        raise FileNotFoundError(f"Required file {text_file} not found")
    with open(text_file, "r", encoding="utf-8") as f:
        papers_text = json.load(f)

    metadata_file = output_dir / "papers_metadata.json"
    if not metadata_file.exists():
        writeLog("warning", logger, f"[ConceptAlignment] {metadata_file} not found. Titles may be missing.")
        metadata_dict = {}
    else:
        with open(metadata_file, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        metadata_dict = {p["paper_id"]: p for p in metadata if "paper_id" in p}

    enriched_corpus = []
    for paper in papers_text:
        pid = paper.get("paper_id")
        if not pid:
            writeLog("warning", logger, "[ConceptAlignment] Paper without paper_id, skipping")
            continue
        title = metadata_dict.get(pid, {}).get("title", f"Untitled ({pid})")
        paper["title"] = title
        enriched_corpus.append(paper)

    writeLog("info", logger, f"[ConceptAlignment] Loaded {len(enriched_corpus)} papers with titles")
    return enriched_corpus


def load_alignment_inputs(input_dir, output_dir) -> tuple[dict, dict]:
    discovered_file = output_dir / "concept_candidates.json"
    if not discovered_file.exists():
        raise FileNotFoundError(f"Required file {discovered_file} not found")
    with open(discovered_file, "r", encoding="utf-8") as f:
        discovered_concepts = json.load(f)

    objective_file = input_dir / "conceptsQuery.json"
    if not objective_file.exists():
        raise FileNotFoundError(f"Required file {objective_file} not found")
    with open(objective_file, "r", encoding="utf-8") as f:
        objective_concepts = json.load(f)

    return discovered_concepts, objective_concepts


def save_alignment(output_dir, result: dict) -> None:
    output_file = output_dir / "aligned_concepts.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    writeLog("info", logger, f"[ConceptAlignment] Saved to {output_file}")


# --------------------------------------------------
# EMBEDDING
# --------------------------------------------------

def encode_alignment_space(
    model:               SentenceTransformer,
    discovered_concepts: dict,
    objective_concepts:  dict,
    focus_terms:         list[str] = None,
) -> tuple[list[str], list[str], np.ndarray, np.ndarray]:
    discovered_texts = [build_concept_text(c) for c in discovered_concepts["concepts"]]
    objective_texts  = [build_objective_text(c, focus_terms) for c in objective_concepts["concepts"]]
    discovered_embeddings = model.encode(
        discovered_texts, normalize_embeddings=True,
        convert_to_numpy=True, batch_size=32, show_progress_bar=False,
    )
    objective_embeddings = model.encode(
        objective_texts, normalize_embeddings=True,
        convert_to_numpy=True, batch_size=32, show_progress_bar=False,
    )
    return discovered_texts, objective_texts, discovered_embeddings, objective_embeddings


def encode_alignment_space_enhanced(
    model:               SentenceTransformer,
    discovered_concepts: dict,
    objective_concepts:  dict,
    corpus:              list,
    focus_terms:         list[str] = None,
) -> tuple[list[str], list[str], np.ndarray, np.ndarray]:
    discovered_texts = []
    for c in discovered_concepts["concepts"]:
        concept_type = determine_concept_type(c)
        text = build_enhanced_concept_text(c, corpus, concept_type)
        discovered_texts.append(text)

    objective_texts = [build_objective_text(c, focus_terms) for c in objective_concepts["concepts"]]

    discovered_embeddings = model.encode(
        discovered_texts, normalize_embeddings=True,
        convert_to_numpy=True, batch_size=32, show_progress_bar=False,
    )
    objective_embeddings = model.encode(
        objective_texts, normalize_embeddings=True,
        convert_to_numpy=True, batch_size=32, show_progress_bar=False,
    )
    return discovered_texts, objective_texts, discovered_embeddings, objective_embeddings


# --------------------------------------------------
# ALIGNMENT COMPUTATION (usa topic_id)
# --------------------------------------------------

def compute_alignment(
    discovered_concepts:   dict,
    objective_concepts:    dict,
    discovered_embeddings: np.ndarray,
    objective_embeddings:  np.ndarray,
) -> list[dict]:
    similarity_matrix = cosine_similarity(discovered_embeddings, objective_embeddings)
    alignments = []
    for concept_idx, similarities in enumerate(similarity_matrix):
        top_indices = similarities.argsort()[-TOP_K_ASSIGNMENTS:][::-1]
        assignments = [
            {
                "objective_concept_id": int(objective_concepts["concepts"][i]["id"]),
                "objective_query":      objective_concepts["concepts"][i]["query"],
                "score":                float(similarities[i]),
            }
            for i in top_indices
        ]
        alignments.append({
            "topic_id":    int(discovered_concepts["concepts"][concept_idx]["topic_id"]),
            "topic_label": discovered_concepts["concepts"][concept_idx]["topic_label"],
            "assignments": assignments,
        })
    return alignments


# --------------------------------------------------
# RE-RANKING DINÁMICO
# --------------------------------------------------

def rerank_documents_with_concept(
    docs: list[dict],
    concept: dict,
    model: SentenceTransformer,
    focus_terms: list[str] = None,
    percentile_threshold: int = None,
    min_docs: int = None,
    alignment_score: float = None
) -> list[tuple[dict, float]]:
    if percentile_threshold is None: percentile_threshold = RERANK_PERCENTILE
    if min_docs is None: min_docs = MIN_DOCS_AFTER_RERANK
    if not docs:
        return []

    concept_parts = [
        concept.get("name", ""),
        concept.get("query", ""),
        concept.get("description", ""),
    ]
    if focus_terms:
        focus_text = " ".join(focus_terms)
        concept_parts.append(focus_text)
        concept_parts.append(focus_text)

    concept_text = " ".join(concept_parts)
    concept_emb = model.encode(concept_text, normalize_embeddings=True)

    doc_texts = []
    for doc in docs:
        title = doc.get("title", "")
        section_text = doc.get("section_text", "")
        if section_text and len(section_text) > 100:
            body = section_text[:2000]
        else:
            body = doc.get("text", "")[:2000]
        doc_texts.append(f"{title}\n{body}")

    doc_embs = model.encode(doc_texts, normalize_embeddings=True)
    similarities = cosine_similarity([concept_emb], doc_embs)[0]
    scored = [(doc, float(sim)) for doc, sim in zip(docs, similarities)]
    scored.sort(key=lambda x: x[1], reverse=True)

    sim_array = np.array([s for _, s in scored])
    if len(sim_array) == 0:
        return []
    threshold = np.percentile(sim_array, percentile_threshold)

    filtered = [(doc, sim) for doc, sim in scored if sim >= threshold]
    if len(filtered) < min_docs:
        filtered = scored[:min_docs]

    if alignment_score is not None:
        combined = []
        for doc, rerank_score in filtered:
            final_score = ALIGNMENT_SCORE_WEIGHT * alignment_score + RERANK_SCORE_WEIGHT * rerank_score
            combined.append((doc, final_score))
        combined.sort(key=lambda x: x[1], reverse=True)
        filtered = combined

    return filtered


# --------------------------------------------------
# DOCUMENT AGGREGATION (usa topic_id)
# --------------------------------------------------

def aggregate_documents_by_objective_concept(
    discovered_concepts: dict,
    alignments:          list[dict],
    objective_concepts:  dict,
    model:               SentenceTransformer,
    focus_terms:         list[str] = None,
) -> dict[int, list[tuple[dict, float]]]:
    topic_lookup = {c["topic_id"]: c for c in discovered_concepts["concepts"]}
    concept_map = {int(c["id"]): {} for c in objective_concepts["concepts"]}

    for alignment in alignments:
        topic_id = alignment["topic_id"]
        source_concept = topic_lookup.get(topic_id)
        if source_concept is None:
            continue

        for assignment in alignment["assignments"]:
            score = assignment["score"]
            objective_id = assignment["objective_concept_id"]
            if score < ALIGNMENT_SCORE_THRESHOLD:
                continue

            for doc in source_concept["documents"]:
                doc_id = doc["doc_id"]
                existing_score = concept_map[objective_id].get(doc_id, (None, -1))[1]
                if score > existing_score:
                    concept_map[objective_id][doc_id] = (doc, score)

    result = {}
    for concept_id, doc_score_map in concept_map.items():
        docs = [doc for doc, _ in doc_score_map.values()]
        alignment_scores = {doc_id: score for doc_id, (doc, score) in doc_score_map.items()}

        obj_concept = next(
            (c for c in objective_concepts["concepts"] if int(c["id"]) == concept_id),
            None
        )
        if obj_concept is None:
            result[concept_id] = [(doc, None) for doc in docs]
            continue

        reranked = []
        for doc in docs:
            align_score = alignment_scores.get(doc["doc_id"])
            reranked_docs = rerank_documents_with_concept(
                [doc], obj_concept, model, focus_terms,
                alignment_score=align_score
            )
            if reranked_docs:
                reranked.extend(reranked_docs)

        reranked.sort(key=lambda x: x[1], reverse=True)
        result[concept_id] = reranked

    return result


# --------------------------------------------------
# OUTPUT ASSEMBLY (Selección dinámica por calidad)
# --------------------------------------------------

def build_aligned_concepts(
    objective_concepts: dict,
    concept_map:        dict[int, list[tuple[dict, float]]],
    discovered_concepts: dict,
) -> list[dict]:
    aligned_concepts = []
    for concept in objective_concepts["concepts"]:
        concept_id = int(concept["id"])
        doc_score_pairs = concept_map.get(concept_id, [])

        concept_type = determine_concept_type({
            "topic_label": concept.get("name", ""),
            "keywords":    concept.get("description", "").split(),
        })

        # Ordenar por el score combinado (mayor a menor)
        doc_score_pairs.sort(key=lambda x: x[1] if x[1] is not None else 0, reverse=True)

        # --- FILTRO DINÁMICO (Ya no es [:30] a ciegas) ---
        # 1. Filtramos por umbral de calidad mínima
        quality_filtered = [
            (doc, score) for doc, score in doc_score_pairs
            if score is not None and score >= MIN_RERANK_SCORE
        ]

        # 2. Garantizamos un mínimo de documentos si el umbral es muy estricto
        if len(quality_filtered) < MIN_DOCS_PER_CONCEPT:
            quality_filtered = doc_score_pairs[:MIN_DOCS_PER_CONCEPT]

        # 3. Aplicamos un tope máximo de seguridad para no saturar la pipeline
        selected_pairs = quality_filtered[:MAX_DOCS_PER_CONCEPT]

        # --- Construir la salida ---
        documents = []
        scores = []
        for doc, score in selected_pairs:
            documents.append(doc)
            scores.append(float(score) if score is not None else None)

        aligned_concepts.append({
            "concept_id":          concept_id,
            "concept_name":        concept.get("name", ""),
            "concept_query":       concept["query"],
            "concept_description": concept.get("description", ""),
            "concept_type":        concept_type,
            "n_documents":         len(documents),
            "documents":           documents,
            "document_scores":     scores,
        })
    return aligned_concepts


# --------------------------------------------------
# ENTRY POINT
# --------------------------------------------------

def processConceptAlignment():
    load_concept_alignment_config()
    input_dir, output_dir = inicioModulo("processConceptAlignment")

    corpus = load_corpus(output_dir)
    discovered_concepts, objective_concepts = load_alignment_inputs(input_dir, output_dir)

    focus_terms = load_focus_terms(input_dir)

    writeLog("info", logger, f"[ConceptAlignment] Loading embedding model {EMBEDDING_MODEL}")
    model = SentenceTransformer(EMBEDDING_MODEL)

    if USE_ENHANCED_EMBEDDINGS:
        (discovered_texts, objective_texts,
         discovered_embeddings, objective_embeddings) = encode_alignment_space_enhanced(
            model, discovered_concepts, objective_concepts, corpus, focus_terms
        )
        writeLog("info", logger, "[ConceptAlignment] Encoding: ENHANCED (section-weighted)")
    else:
        (discovered_texts, objective_texts,
         discovered_embeddings, objective_embeddings) = encode_alignment_space(
            model, discovered_concepts, objective_concepts, focus_terms
        )
        writeLog("info", logger, "[ConceptAlignment] Encoding: baseline (no sections)")

    alignments = compute_alignment(
        discovered_concepts, objective_concepts,
        discovered_embeddings, objective_embeddings,
    )

    concept_map = aggregate_documents_by_objective_concept(
        discovered_concepts, alignments, objective_concepts, model, focus_terms
    )

    aligned_concepts = build_aligned_concepts(
        objective_concepts, concept_map, discovered_concepts,
    )

    result = {
        "schema_version":       "1.5",
        "alignment_threshold":  ALIGNMENT_SCORE_THRESHOLD,
        "use_enhanced":         USE_ENHANCED_EMBEDDINGS,
        "selection_policy":     "dynamic_quality", # Indicador de que ya no es hard-limit
        "rerank_percentile":    RERANK_PERCENTILE,
        "min_docs_after_rerank": MIN_DOCS_AFTER_RERANK,
        "min_rerank_score":     MIN_RERANK_SCORE,
        "focus_terms_used":     bool(focus_terms),
        "score_combination":    {"alpha": ALIGNMENT_SCORE_WEIGHT, "beta": RERANK_SCORE_WEIGHT},
        "n_objective_concepts": len(objective_concepts["concepts"]),
        "aligned_concepts":     aligned_concepts,
        "alignments":           alignments,
        "section_weights_used": SECTION_WEIGHTS,
    }

    save_alignment(output_dir, result)

    writeLog("info", logger, "\n" + "=" * 80)
    writeLog("info", logger, "📋 RESUMEN DE CONCEPTOS ALINEADOS")
    writeLog("info", logger, "=" * 80)

    total_docs = 0
    for ac in aligned_concepts:
        doc_ids = [doc.get("doc_id", "unknown") for doc in ac.get("documents", [])]
        doc_ids_str = ", ".join(doc_ids) if doc_ids else "ninguno"
        total_docs += ac.get("n_documents", 0)
        writeLog("info", logger, f"\n🔹 Concepto {ac['concept_id']}:")
        writeLog("info", logger, f"   Pregunta: {ac['concept_query']}")
        writeLog("info", logger, f"   📄 Documentos: {ac['n_documents']} (Dinámico por calidad)")
        writeLog("info", logger, f"   🆔 IDs: {doc_ids_str}")

    writeLog("info", logger, "\n" + "-" * 80)
    writeLog("info", logger, f"✅ Total de documentos alineados: {total_docs}")
    writeLog("info", logger, "=" * 80)

    return result


if __name__ == "__main__":
    processConceptAlignment()