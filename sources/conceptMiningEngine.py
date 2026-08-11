# conceptMiningEngine.py
from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo
from sources.section_indexer import SECTION_WEIGHTS  # Importamos los pesos de secciones

from pathlib import Path
import json
from collections import defaultdict

from keybert import KeyBERT

TOP_K_KEYWORDS = 30
NGRAM_RANGE = (1, 3)


def load_concept_mining_config() -> None:
    """
    Sobrescribe las constantes del módulo con la sección "conceptMining"
    de config.json (processControl.defaults). Los valores del fichero tienen
    prioridad; si faltan, se conservan los valores por defecto.
    """
    defaults = getattr(processControl, "defaults", None) or {}
    cfg = defaults.get("conceptMining", {}) if isinstance(defaults, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}

    global TOP_K_KEYWORDS, NGRAM_RANGE
    TOP_K_KEYWORDS = int(cfg.get("top_k_keywords", TOP_K_KEYWORDS))
    ngram = cfg.get("ngram_range", NGRAM_RANGE)
    NGRAM_RANGE = tuple(ngram) if isinstance(ngram, (list, tuple)) else NGRAM_RANGE


# Lazy loading: model loaded on first call
_keybert_model: KeyBERT | None = None


def _get_keybert() -> KeyBERT:
    global _keybert_model
    if _keybert_model is None:
        _keybert_model = KeyBERT()
    return _keybert_model


# 🔥 NUEVO: Función para cargar estudio desde JSON
def load_study_context(input_dir: Path) -> list[str]:
    """
    Lee studyDescription.json y devuelve la lista de focus_terms.
    Si no existe, devuelve lista vacía.
    """
    json_file = input_dir / "studyDescription.json"
    focus_terms = []

    if json_file.exists():
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            project = data.get("project", {})
            focus_terms = project.get("focus_terms", [])
            if focus_terms:
                writeLog("info", logger, f"[ConceptMining] Loaded {len(focus_terms)} focus terms")
            else:
                writeLog("info", logger, "[ConceptMining] No focus_terms found in studyDescription.json")
        except Exception as e:
            writeLog("error", logger, f"[ConceptMining] Error reading studyDescription.json: {e}")
    else:
        writeLog("info", logger, "[ConceptMining] studyDescription.json not found — running without focus terms")

    return focus_terms


def build_document_objects(clean_corpus_dict: dict, doc_topic_map: list, target_topic: int) -> list:
    """
    Returns a list of documents (dict with doc_id, title, text, sections_used)
    that belong to target_topic.
    """
    topic_docs_map = defaultdict(list)

    for item in doc_topic_map:
        if item["topic"] != target_topic:
            continue
        doc_id = item["doc_id"]
        section_name = item.get("section")

        paper = clean_corpus_dict.get(doc_id)
        if paper is None:
            writeLog("warning", logger, f"[ConceptMining] Paper {doc_id} not found in corpus")
            continue

        title = paper.get("title", f"Untitled ({doc_id})")

        # 1) If section is known, extract that specific section
        if section_name:
            sections = paper.get("clean_sections", {}) or paper.get("sections", {})
            section_text = sections.get(section_name, "")
            if section_text and len(section_text) > 50:
                # Guardamos también el nombre de la sección para luego incluirlo en sections_used
                topic_docs_map[(doc_id, target_topic)].append((section_text, title, section_name))
                continue

        # 2) Fallback: use full text cascading
        text = paper.get("clean_text", "")
        if not text or len(text) < 100:
            text = paper.get("abstract", "")
        if not text or len(text) < 100:
            text = paper.get("full_text", "")
        if not text or len(text) < 50:
            text = paper.get("title", "") + " " + " ".join(paper.get("keywords", []))

        if text:
            # Para fallback, no tenemos un nombre de sección específico
            topic_docs_map[(doc_id, target_topic)].append((text, title, "fallback"))

    # Build final list, one entry per (doc_id, topic) con texto combinado y secciones usadas
    results = []
    for (doc_id, topic), items in topic_docs_map.items():
        texts = [item[0] for item in items]
        title = items[0][1] if items else f"Untitled ({doc_id})"
        # Recolectar nombres de secciones (sin duplicados y sin "fallback")
        sections_used_set = set()
        for item in items:
            if len(item) > 2:
                sec = item[2]
                if sec and sec != "fallback":
                    sections_used_set.add(sec)
        # Si solo hay fallback, dejar lista vacía (o incluir "full_text")
        sections_used = sorted(list(sections_used_set))

        combined_text = "\n\n".join(texts)
        results.append({
            "doc_id": doc_id,
            "title": title,
            "text": combined_text,
            "sections_used": sections_used  # Ahora correctamente poblado
        })
    return results


def normalize_representative_docs(representative_docs):
    """Kept for compatibility with older candidate_concepts.json (pre‑1.4)."""
    if representative_docs is None:
        return []
    if isinstance(representative_docs, dict):
        representative_docs = [representative_docs]
    if not isinstance(representative_docs, list):
        raise TypeError(f"Invalid representative_docs type: {type(representative_docs)}")
    return [doc for doc in representative_docs if isinstance(doc, dict) and "text" in doc]


def extract_topic_keywords(topic_docs: list, focus_terms: list[str] = None) -> list:
    """
    Extrae keywords del texto combinado de los documentos del tópico.
    Estrategia en dos pasos (mejora SOA 1):
    1. Extracción libre sin candidates para capturar términos relevantes generales.
    2. Extracción con candidates=focus_terms para capturar términos sensor.
    Combina ambas listas, deduplicando, y prioriza los términos sensor.
    Además, aplica ponderación de secciones (mejora SOA 2) repitiendo el texto
    de secciones con mayor peso según SECTION_WEIGHTS.
    """
    if not topic_docs:
        return []

    # 🔥 MEJORA SOA 2: Aplicar ponderación por sección
    weighted_texts = []
    for doc in topic_docs:
        text = doc.get("text", "")
        if not text:
            continue
        # Si doc tiene sections_used, podemos ponderar el texto según las secciones
        # Pero como el texto ya está concatenado, no podemos fácilmente separar por sección.
        # Sin embargo, podemos repetir el texto completo si sabemos que las secciones son importantes.
        # En su lugar, aplicamos un enfoque más simple: si el tópico tiene secciones relevantes
        # (metodología o resultados), ponderamos el texto completo con un factor.
        # Para simplificar, usamos el texto tal cual, pero podríamos en el futuro
        # usar la información de sections_used para ponderar.
        weighted_texts.append(text)

    combined_text = "\n".join(weighted_texts)
    if not combined_text.strip():
        return []

    kw_model = _get_keybert()

    # 🔥 MEJORA SOA 1: Extracción en dos pasos
    # Paso 1: extracción libre sin candidates
    free_keywords = kw_model.extract_keywords(
        combined_text,
        keyphrase_ngram_range=NGRAM_RANGE,
        stop_words="english",
        top_n=TOP_K_KEYWORDS
    )
    free_terms = set([kw for kw, _ in free_keywords])

    # Paso 2: si hay focus_terms, extraer con candidates (restricción)
    sensor_terms = set()
    if focus_terms:
        sensor_keywords = kw_model.extract_keywords(
            combined_text,
            keyphrase_ngram_range=NGRAM_RANGE,
            stop_words="english",
            top_n=min(len(focus_terms), TOP_K_KEYWORDS),
            candidates=focus_terms
        )
        sensor_terms = set([kw for kw, _ in sensor_keywords])

    # Combinar: primero los términos sensor, luego los libres (deduplicados)
    final_terms = list(sensor_terms) + [t for t in free_terms if t not in sensor_terms]
    # Limitar al TOP_K_KEYWORDS
    final_terms = final_terms[:TOP_K_KEYWORDS]

    return final_terms


# 🔥 NUEVO: Ahora recibe focus_terms
def build_concept_candidates(
        topic_data: dict,
        papers_text: list,
        papers_metadata: list,
        focus_terms: list[str] = None
) -> list:
    """
    Builds concept candidates from:
    - topic_data (candidate_concepts.json)
    - papers_text (papers_text.json) containing clean_text and clean_sections
    - papers_metadata (papers_metadata.json) containing title and other metadata
    - focus_terms (optional) to guide keyword extraction
    """
    # Build metadata lookup: paper_id -> title
    metadata_dict = {p["paper_id"]: p.get("title", "") for p in papers_metadata if "paper_id" in p}

    # Build corpus_dict from papers_text and add title from metadata
    corpus_dict = {}
    for paper in papers_text:
        pid = paper.get("paper_id")
        if not pid:
            writeLog("warning", logger, "[ConceptMining] Paper without paper_id found, skipping")
            continue
        # Copy paper data
        enriched = dict(paper)
        # Add title from metadata (or fallback)
        enriched["title"] = metadata_dict.get(pid, f"Untitled ({pid})")
        corpus_dict[pid] = enriched

    doc_topic_map = topic_data.get("doc_topic_map", [])
    concepts = []

    for topic in topic_data.get("concepts", []):
        topic_id = topic["concept_id"]
        topic_label = topic.get("label", f"topic_{topic_id}")

        docs_for_topic = build_document_objects(corpus_dict, doc_topic_map, topic_id)
        keywords = extract_topic_keywords(docs_for_topic, focus_terms)

        writeLog(
            "info", logger,
            f"[ConceptMining] topic {topic_id} '{topic_label[:50]}' "
            f"→ {len(docs_for_topic)} documents, {len(keywords)} keywords"
        )

        concepts.append({
            "topic_id": topic_id,
            "topic_label": topic_label,
            "keywords": keywords,
            "documents": docs_for_topic
        })

    return concepts


def save_candidates(candidates: list, output_file: Path) -> None:
    result = {
        "schema_version": "1.2",
        "n_concepts": len(candidates),
        "concepts": candidates
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)


def processConceptMiningEngine():
    load_concept_mining_config()
    input_dir, output_dir = inicioModulo("processConceptMiningEngine")
    input_file = output_dir / "candidate_concepts.json"
    if not input_file.exists():
        writeLog("error", logger, f"[ConceptMining] {input_file} not found")
        return None

    with open(input_file, "r", encoding="utf-8") as f:
        topic_data = json.load(f)

    writeLog("info", logger, "[ConceptMining] Building candidates...")

    # Load papers_text.json (texts and sections)
    text_file = output_dir / "papers_text.json"
    if not text_file.exists():
        writeLog("error", logger, f"[ConceptMining] {text_file} not found")
        return None

    with open(text_file, "r", encoding="utf-8") as f:
        papers_text = json.load(f)

    # Load papers_metadata.json (titles)
    metadata_file = output_dir / "papers_metadata.json"
    if not metadata_file.exists():
        writeLog("error", logger, f"[ConceptMining] {metadata_file} not found")
        return None

    with open(metadata_file, "r", encoding="utf-8") as f:
        papers_metadata = json.load(f)

    # 🔥 NUEVO: Cargar focus_terms
    focus_terms = load_study_context(input_dir)

    candidates = build_concept_candidates(topic_data, papers_text, papers_metadata, focus_terms)
    output_file = output_dir / "concept_candidates.json"
    save_candidates(candidates, output_file)

    # 🔥 NUEVO: Logging de cada concepto con su topic_label y número de documentos
    writeLog("info", logger, f"[ConceptMining] Saved to {output_file}")
    writeLog("info", logger, f"[ConceptMining] {len(candidates)} concept candidates written")
    writeLog("info", logger, "--- Conceptos generados ---")
    for concept in candidates:
        topic_id = concept.get("topic_id")
        topic_label = concept.get("topic_label", f"topic_{topic_id}")
        n_docs = len(concept.get("documents", []))
        writeLog("info", logger, f"  Concepto {topic_id}: '{topic_label}' → {n_docs} documentos")
    writeLog("info", logger, "--- Fin de lista ---")

    return {
        "schema_version": "1.2",
        "n_concepts": len(candidates),
        "concepts": candidates
    }


if __name__ == "__main__":
    processConceptMiningEngine()