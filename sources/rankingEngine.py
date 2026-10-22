# rankingEngine.py
from sources.common.common import logger, processControl, writeLog
from sources.common.streams import load_streams
from sources.common.utils import inicioModulo, load_url_blacklist, is_url_blacklisted, sha1, normalized_title

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

def rank_papers_embeddings(papers: list[dict], query_text: str,
                           similarity_threshold: float = 0.0) -> list[dict]:
    """
    Calcula similitud de coseno entre la query y cada paper, filtra por
    umbral de similitud y devuelve los papers que lo SUPERAN, ordenados
    de mayor a menor similitud.

    El umbral se aplica ANTES del corte top_k, de modo que sabemos
    exactamente cuántos papers son relevantes y cuántos se quedan fuera
    solo por el límite de top_k.
    """
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

    # Aplicar umbral de similitud ANTES del top_k
    ranked = [
        {**paper, "relevance_score": float(score)}
        for paper, score in zip(rankable, similarities)
        if float(score) >= similarity_threshold
    ]
    ranked.sort(key=lambda x: x["relevance_score"], reverse=True)

    writeLog("info", logger,
        f"[RankingEngine] {len(ranked)}/{len(rankable)} papers superan el "
        f"umbral de similitud ({similarity_threshold:.3f})")
    return ranked


def select_top_k(ranked_papers: list[dict], k: int = TOP_K) -> list[dict]:
    return ranked_papers[:k]


def _get_global_id(paper: dict) -> str | None:
    """Calcula el global_id de un paper igual que en manualIngestion / searchMerge."""
    doi = (paper.get("doi") or "").strip().lower()
    if doi:
        return f"doi:{doi}"
    title = normalized_title(paper.get("title", ""))
    if title:
        return f"title:{sha1(title)}"
    return None


def _load_manual_global_ids(input_dir: Path) -> set[str]:
    """
    Lee manual_papers.json (si existe) y devuelve el set de global_ids
    de los papers manuales. Se usa para excluirlos del ranking antes de
    aplicar top_k, de modo que no ocupen cupo de búsqueda y dejen espacio
    para el siguiente paper relevante.
    """
    manual_json = input_dir / "manual_papers" / "manual_papers.json"
    if not manual_json.exists():
        return set()

    try:
        with open(manual_json, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except Exception:
        return set()

    ids: set[str] = set()
    for entry in entries:
        gid = _get_global_id(entry)
        if gid:
            ids.add(gid)
    return ids


def _stream_ranking_query(stream: dict, study_query: str) -> str:
    """Query de embedding para rankear un flujo: ranking_query > query > estudio."""
    return (stream.get("ranking_query") or stream.get("query") or study_query).strip()


def _rank_by_streams(papers: list[dict], streams: list[dict], study_query: str,
                     similarity_threshold: float = 0.0) -> tuple[list[dict], list[dict]]:
    """
    Rankea por flujo con cuota independiente y une los resultados.

    Cada flujo solo puntúa los papers etiquetados con su nombre (o todos si el
    corpus no trae etiquetas y hay un único flujo legacy). Así un flujo "nlp"
    con su propia query no compite contra el ancla fisiológica del flujo
    "physio".

    Devuelve una tupla: (papers dentro de top_k, papers que pasaron el umbral
    pero se quedaron fuera por el límite de top_k).
    """
    tagged = any(p.get("query_streams") for p in papers)

    selected: dict[str, dict] = {}
    order: list[str] = []
    outside_top_k: list[dict] = []
    for stream in streams:
        name = stream["name"]
        if not tagged and len(streams) == 1:
            subset = papers
        else:
            subset = [p for p in papers if name in (p.get("query_streams") or [])]
        if not subset:
            continue

        query_text = _stream_ranking_query(stream, study_query)
        if not query_text:
            continue

        # rank_papers_embeddings ya aplicó el umbral de similitud
        ranked = rank_papers_embeddings(subset, query_text,
                                        similarity_threshold=similarity_threshold)
        top_k = int(stream["top_k"]) if stream.get("top_k") else TOP_K
        added = 0
        for i, paper in enumerate(ranked):
            gid = paper.get("global_id") or paper.get("paper_id")
            if gid in selected:
                continue
            if i < top_k:
                selected[gid] = paper
                order.append(gid)
                added += 1
            else:
                # Pasó el umbral pero se quedó fuera por el límite de top_k
                outside_top_k.append(paper)
        writeLog("info", logger,
                 f"[RankingEngine] Flujo '{name}': {len(subset)} candidatos -> "
                 f"{added} seleccionados (top_k={top_k}), "
                 f"{len(ranked) - added} superaron el umbral pero quedaron fuera por top_k")

    return [selected[gid] for gid in order], outside_top_k


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

    # --- URL/DOMAIN BLACKLIST FILTER (antes del ranking) ---
    # Los papers cuyo landing_page/pdf_url coincida con un dominio blacklisteado
    # se excluyen antes del scoring para no ocupar cupo de top_k.
    url_blacklist = load_url_blacklist(output_dir)
    if url_blacklist:
        before = len(papers)
        papers = [p for p in papers if not is_url_blacklisted(p, url_blacklist)]
        removed = before - len(papers)
        writeLog("info", logger,
                 f"[RankingEngine] URL blacklist aplicada: {removed} papers excluidos "
                 f"por dominio inaccesible (quedan {len(papers)})")
        if removed:
            writeLog("info", logger,
                     f"[RankingEngine] Dominios blacklisteados: {sorted(url_blacklist)}")

    # --- EXCLUIR PAPERS MANUALES ANTES DEL RANKING ---
    # Los papers ya definidos en manual_papers.json no ocupan cupo de top_k;
    # se añaden después vía manualIngestion sin límite. Esto garantiza que
    # si límite=130 y hay 10 manuales, el total puede ser 140 (130 búsqueda +
    # 10 manuales), y que un duplicado búsqueda↔manual libera su cupo para
    # el siguiente paper relevante de búsqueda.
    manual_global_ids = _load_manual_global_ids(input_dir)
    if manual_global_ids:
        before = len(papers)
        papers = [p for p in papers if p.get("global_id") not in manual_global_ids]
        removed = before - len(papers)
        writeLog("info", logger,
                 f"[RankingEngine] Papers manuales excluidos del ranking: {removed} "
                 f"(no ocupan cupo de top_k={TOP_K}), quedan {len(papers)}")

    # --- PAPER BLACKLIST POR BAJO CONTENIDO DE TEXTO ---
    # Los papers cuyo paper_id aparezca en paper_blacklist.ids de config.json
    # se excluyen antes del ranking para no ocupar cupo de top_k.
    # Esta blacklist se alimenta automáticamente desde corpusCleaning
    # (papers con <500 palabras tras limpieza se guardan en low_text_papers.json
    # y el operador puede añadirlos manualmente a config.json).
    defaults = getattr(processControl, "defaults", None) or {}
    paper_bl_cfg = defaults.get("paper_blacklist", {}) if isinstance(defaults, dict) else {}
    paper_blacklist_ids = set(paper_bl_cfg.get("ids", []) or [])
    paper_bl_reason = paper_bl_cfg.get("reason", "")
    if paper_blacklist_ids:
        before = len(papers)
        papers = [p for p in papers if p.get("paper_id") not in paper_blacklist_ids]
        removed = before - len(papers)
        writeLog("info", logger,
                 f"[RankingEngine] Papers excluidos por paper_blacklist: {removed} "
                 f"(bajo contenido de texto u otro motivo), quedan {len(papers)}")
        if removed:
            writeLog("info", logger,
                     f"[RankingEngine] paper_blacklist reason: {paper_bl_reason}")
            writeLog("info", logger,
                     f"[RankingEngine] IDs blacklisteados: {sorted(paper_blacklist_ids)}")

    study_query = build_query_from_study_description(study_desc)
    if not study_query.strip():
        writeLog("error", logger,
            "[RankingEngine] Empty query built from studyDescription.json — "
            "check 'objectives', 'keywords', 'focus_terms' fields")
        return None

    search_config = processControl.defaults.get("search", {})
    streams = load_streams(search_config)

    # Umbral de similitud: se aplica ANTES del top_k.
    # Debe venir obligatoriamente de config.json > defaults.ranking.similarity_threshold.
    # Si no existe, se lanza warning y se usa 0.00 (sin filtro).
    defaults = getattr(processControl, "defaults", None) or {}
    ranking_cfg = defaults.get("ranking", {}) if isinstance(defaults, dict) else {}
    if "similarity_threshold" not in ranking_cfg:
        writeLog("warning", logger,
                 "[RankingEngine] 'similarity_threshold' no está definido en "
                 "config.json > defaults.ranking. Se usará 0.00 (sin filtro de similitud).")
        similarity_threshold = 0.0
    else:
        similarity_threshold = float(ranking_cfg["similarity_threshold"])
        writeLog("info", logger,
                 f"[RankingEngine] Umbral de similitud: {similarity_threshold:.3f}")

    top_k, outside_top_k = _rank_by_streams(papers, streams, study_query,
                                            similarity_threshold=similarity_threshold)

    if not top_k:
        writeLog("warning", logger, "[RankingEngine] No papers selected by any stream")
        return []

    # Guardar papers dentro de top_k
    output_file = output_dir / "ranked_papers.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(top_k, f, indent=2, ensure_ascii=False)

    # Guardar papers que pasaron el umbral pero se quedaron fuera por top_k
    outside_file = output_dir / "ranking_outside_top_k.json"
    with open(outside_file, "w", encoding="utf-8") as f:
        json.dump(outside_top_k, f, indent=2, ensure_ascii=False)

    # Guardar estadísticas de ranking para el informe PRISMA
    # Contar cuántos papers pasaron el umbral en total (top_k + outside)
    total_passed_threshold = len(top_k) + len(outside_top_k)
    stats = {
        "total_evaluated": len(papers),
        "passed_threshold": total_passed_threshold,
        "outside_top_k": len(outside_top_k),
        "top_k_final": len(top_k),
        "similarity_threshold": similarity_threshold,
    }
    stats_file = output_dir / "ranking_stats.json"
    with open(stats_file, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    writeLog("info", logger,
        f"[RankingEngine] Saved {len(top_k)} ranked papers to {output_file} "
        f"(from {len(papers)} total, {len(streams)} stream(s))")
    writeLog("info", logger,
        f"[RankingEngine] {total_passed_threshold} papers superaron el umbral "
        f"({similarity_threshold:.3f}); {len(outside_top_k)} se quedaron fuera por top_k")

    return top_k


if __name__ == "__main__":
    processRankingEngine()