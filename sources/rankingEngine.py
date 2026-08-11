# rankingEngine.py
from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo

from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer
import json
from pathlib import Path

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

# CHANGED: was "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
# a multilingual paraphrase model — a different task (cross-lingual
# paraphrase detection) from retrieval, and unnecessary since the
# scientific literature indexed by Scopus/WoS/IEEE is overwhelmingly
# English regardless of the research project's domain. bge-base-en-v1.5
# is trained on query-passage retrieval pairs and is used consistently
# across the rest of the SLIP pipeline (discoveryEngine, conceptMining,
# conceptAlignment, clusterEvidences). This is the first relevance filter
# in the pipeline — embedding quality here determines what even reaches
# later stages.
EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"

TOP_K = 100

# Minimum non-empty length for title/abstract combined for a record to
# be considered for ranking. Records below this are excluded rather
# than scored, since an empty document produces a near-meaningless
# similarity score that competes unfairly with real content.
MIN_TEXT_LENGTH = 20


def load_ranking_config() -> None:
    """
    Sobrescribe las constantes del módulo con la sección "ranking" de
    config.json (processControl.defaults). Los valores del fichero tienen
    prioridad; si faltan, se conservan los valores por defecto.
    """
    defaults = getattr(processControl, "defaults", None) or {}
    cfg = defaults.get("ranking", {}) if isinstance(defaults, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}

    global EMBEDDING_MODEL, TOP_K, MIN_TEXT_LENGTH
    EMBEDDING_MODEL = cfg.get("embedding_model", EMBEDDING_MODEL)
    TOP_K = int(cfg.get("top_k", TOP_K))
    MIN_TEXT_LENGTH = int(cfg.get("min_text_length", MIN_TEXT_LENGTH))


# --------------------------------------------------
# MODEL — lazy loaded
# CHANGED: was instantiated at module import time
# (`model = SentenceTransformer(MODEL_NAME)`), which loads the model into
# VRAM as soon as this module is imported, regardless of whether
# processRankingEngine() actually runs in that execution. Same pattern
# applied to conceptMiningEngine (KeyBERT) and processConceptEvidence.
# --------------------------------------------------

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model


# --------------------------------------------------
# TEXT BUILDERS
# --------------------------------------------------

def _safe(value) -> str:
    """
    CHANGED: new helper. paper.get(field, "") only falls back to the
    default when the KEY IS ABSENT — it does NOT catch a key that
    exists with value None, which is exactly the shape of real records
    like wos_1 (title: null, abstract: null). Without this, build_text
    embedded the literal string "None" into the document text for every
    record missing metadata, silently corrupting their ranking.
    """
    return value if isinstance(value, str) else ""


def build_document_text(paper: dict) -> str:
    title    = _safe(paper.get("title"))
    abstract = _safe(paper.get("abstract"))
    keywords = " ".join(_safe(k) for k in (paper.get("keywords") or []))

    return f"""
    TITLE:
    {title}

    ABSTRACT:
    {abstract}

    KEYWORDS:
    {keywords}
    """


def has_sufficient_text(paper: dict) -> bool:
    """
    NEW: records with no title and no abstract (e.g. wos_1, where the
    source indexer returned no metadata at all) carry no real signal.
    Embedding an almost-empty document and letting it compete in the
    ranking produces an arbitrary similarity score — neither reliably
    high nor low — which is noise, not a meaningful exclusion. These
    records are filtered out before embedding instead.
    """
    title    = _safe(paper.get("title"))
    abstract = _safe(paper.get("abstract"))
    return len(title) + len(abstract) >= MIN_TEXT_LENGTH


def build_query_from_study_description(study_desc: dict) -> str:
    """
    Builds a query string from the studyDescription.json structure.
    Emphasizes keywords and focus_terms by repeating them — a simple
    but effective bias technique already used elsewhere in the pipeline
    (see discoveryEngine's SEED_PREPEND_WEIGHT).

    Domain-agnostic by design: focus_terms is whatever the research
    project defines (sensor vocabulary for ANNOTATE, evaluation-design
    vocabulary for VALIDATE, etc.) — this function has no awareness of
    what those terms mean.
    """
    project = study_desc.get("project", {})
    objectives  = " ".join(project.get("objectives", []))
    keywords    = " ".join(project.get("keywords", []))
    focus_terms = " ".join(project.get("focus_terms", []))

    query_parts = [
        objectives,
        keywords, keywords,
        focus_terms, focus_terms, focus_terms,
    ]
    return " ".join(p for p in query_parts if p)


# --------------------------------------------------
# RANKING
# --------------------------------------------------

def rank_papers_embeddings(papers: list[dict], query_text: str) -> list[dict]:
    model = _get_model()

    # CHANGED: split into rankable vs excluded before embedding.
    # Excluded records are still written to the log so nothing silently
    # disappears from view.
    rankable = [p for p in papers if has_sufficient_text(p)]
    excluded = [p for p in papers if not has_sufficient_text(p)]

    if excluded:
        writeLog("info", logger,
            f"[RankingEngine] Excluded {len(excluded)} record(s) with "
            f"insufficient title/abstract text "
            f"(e.g. {[p.get('paper_id') for p in excluded[:5]]})")

    if not rankable:
        writeLog("warning", logger, "[RankingEngine] No papers with sufficient text to rank")
        return []

    documents = [build_document_text(p) for p in rankable]

    query_embedding = model.encode(query_text, normalize_embeddings=True)
    document_embeddings = model.encode(
        documents,
        normalize_embeddings=True,
        batch_size=32,
        show_progress_bar=True,
    )

    similarities = cosine_similarity([query_embedding], document_embeddings)[0]

    ranked = [
        {**paper, "relevance_score": float(score)}
        for paper, score in zip(rankable, similarities)
    ]
    ranked.sort(key=lambda x: x["relevance_score"], reverse=True)
    return ranked


def select_top_k(ranked_papers: list[dict], k: int = TOP_K) -> list[dict]:
    return ranked_papers[:k]


# --------------------------------------------------
# ENTRY POINT
# --------------------------------------------------

def processRankingEngine():
    load_ranking_config()
    input_dir, output_dir = inicioModulo("processRankingEngine")

    # CHANGED: defensive file loading with descriptive errors, consistent
    # with the pattern used in trainingMaterials. Previously a missing
    # file raised a bare FileNotFoundError with no pipeline context.
    canonical_file = output_dir / "canonical.json"
    if not canonical_file.exists():
        writeLog("error", logger, f"[RankingEngine] {canonical_file} not found")
        return None

    with open(canonical_file, "r", encoding="utf-8") as f:
        papers = json.load(f)

    study_desc_file = input_dir / "studyDescription.json"
    if not study_desc_file.exists():
        writeLog("error", logger, f"[RankingEngine] {study_desc_file} not found")
        return None

    with open(study_desc_file, "r", encoding="utf-8") as f:
        study_desc = json.load(f)

    writeLog("info", logger, f"[RankingEngine] Loaded {len(papers)} candidate papers")

    query_text = build_query_from_study_description(study_desc)
    if not query_text.strip():
        writeLog("error", logger,
            "[RankingEngine] Empty query built from studyDescription.json — "
            "check 'objectives', 'keywords', 'focus_terms' fields")
        return None

    ranked = rank_papers_embeddings(papers, query_text)
    top_k = select_top_k(ranked, k=TOP_K)

    output_file = output_dir / "ranked_papers.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(top_k, f, indent=2, ensure_ascii=False)

    writeLog("info", logger,
        f"[RankingEngine] Saved {len(top_k)} ranked papers to {output_file} "
        f"(from {len(ranked)} rankable of {len(papers)} total)")

    return top_k


if __name__ == "__main__":
    processRankingEngine()