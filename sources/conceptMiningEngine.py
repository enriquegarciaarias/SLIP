# conceptMiningEngine.py
from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo
from sources.section_indexer import SECTION_WEIGHTS  # Importamos los pesos de secciones

from pathlib import Path
import json
from collections import defaultdict
from typing import Dict

import numpy as np
from keybert import KeyBERT
from sentence_transformers import SentenceTransformer, util

TOP_K_KEYWORDS = 30
NGRAM_RANGE = (1, 3)
# Modelo de embeddings para KeyBERT. Se unifica con el resto del pipeline
# (bge-base) para no mezclar espacios vectoriales distintos.
EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
# Umbral de similitud coseno para deduplicación semántica de keywords
SEMANTIC_DEDUP_THRESHOLD = 0.85
SEMANTIC_DEDUP_ENABLED = True
# Modelo cargado perezosamente para embeddings de deduplicación
_semantic_model: SentenceTransformer | None = None


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

    global TOP_K_KEYWORDS, NGRAM_RANGE, EMBEDDING_MODEL, SEMANTIC_DEDUP_THRESHOLD, SEMANTIC_DEDUP_ENABLED
    TOP_K_KEYWORDS = int(cfg.get("top_k_keywords", TOP_K_KEYWORDS))
    ngram = cfg.get("ngram_range", NGRAM_RANGE)
    NGRAM_RANGE = tuple(ngram) if isinstance(ngram, (list, tuple)) else NGRAM_RANGE
    EMBEDDING_MODEL = cfg.get("embedding_model", EMBEDDING_MODEL)
    SEMANTIC_DEDUP_THRESHOLD = float(cfg.get("semantic_dedup_threshold", SEMANTIC_DEDUP_THRESHOLD))
    SEMANTIC_DEDUP_ENABLED = bool(cfg.get("semantic_dedup_enabled", SEMANTIC_DEDUP_ENABLED))


# 🔥 NUEVO: Cargar papers estructurados
def load_structured_papers(output_dir: Path) -> Dict[str, dict]:
    """Carga papers_structured.json y retorna dict paper_id -> structured_data."""
    structured_file = output_dir / "papers_structured.json"
    if not structured_file.exists():
        writeLog("info", logger, "[ConceptMining] No papers_structured.json found — running without structured data")
        return {}
    try:
        with open(structured_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            structured = {p.get("paper_id"): p for p in data if p.get("paper_id")}
            writeLog("info", logger, f"[ConceptMining] Loaded {len(structured)} structured papers")
            return structured
    except Exception as e:
        writeLog("warning", logger, f"[ConceptMining] Could not load papers_structured.json: {e}")
    return {}


# 🔥 NUEVO: Extraer términos estructurados para usar como candidates en KeyBERT
def extract_structured_terms(structured: dict) -> list[str]:
    """Extrae términos clave de los campos estructurados para guiar KeyBERT."""
    if not structured or structured.get("skipped"):
        return []
    
    terms = []
    
    # Contribuciones
    for c in structured.get("contributions", []):
        terms.append(c)
    
    # Metodología
    method = structured.get("methodology", {})
    if isinstance(method, dict):
        for k in ["approach", "architecture"]:
            if method.get(k):
                terms.append(method[k])
        if method.get("datasets"):
            terms.extend(method["datasets"])
        if method.get("baselines"):
            terms.extend(method["baselines"])
        if method.get("metrics"):
            terms.extend(method["metrics"])
        if method.get("signal_modalities"):
            terms.extend(method["signal_modalities"])
    
    # Resultados
    results = structured.get("results", {})
    if isinstance(results, dict):
        if results.get("main"):
            terms.append(results["main"])
        if results.get("ablation"):
            terms.extend(results["ablation"])
        if results.get("key_numbers"):
            terms.extend(results["key_numbers"])
        if results.get("tables_referenced"):
            terms.extend(results["tables_referenced"])
    
    # Limitaciones y future work (menos prioritarios)
    for l in structured.get("limitations", [])[:3]:
        terms.append(l)
    for f in structured.get("future_work", [])[:3]:
        terms.append(f)
    
    # Filtrar: solo términos con cierta longitud y no vacíos
    filtered = [t.strip() for t in terms if t and len(t.strip()) > 3]
    
    # 🔥 MEJORA: Priorizar campos de alto valor técnico y extraer n-grams limpios
    # Orden de prioridad: key_numbers, metrics, datasets, signal_modalities, baselines, tables, approach/architecture
    high_value_terms = []
    
    # 1. Key numbers (métricas numéricas, tablas) - MÁS ALTA PRIORIDAD
    results = structured.get("results", {})
    if isinstance(results, dict):
        if results.get("key_numbers"):
            high_value_terms.extend(results["key_numbers"][:10])  # Top 10
        if results.get("tables_referenced"):
            high_value_terms.extend(results["tables_referenced"][:5])
        if results.get("main"):
            high_value_terms.append(results["main"])
        if results.get("ablation"):
            high_value_terms.extend(results["ablation"][:5])
    
    # 2. Métricas, datasets, signals, baselines
    method = structured.get("methodology", {})
    if isinstance(method, dict):
        if method.get("metrics"):
            high_value_terms.extend(method["metrics"][:10])
        if method.get("datasets"):
            high_value_terms.extend(method["datasets"][:10])
        if method.get("signal_modalities"):
            high_value_terms.extend(method["signal_modalities"][:10])
        if method.get("baselines"):
            high_value_terms.extend(method["baselines"][:10])
        if method.get("approach"):
            high_value_terms.append(method["approach"])
        if method.get("architecture"):
            high_value_terms.append(method["architecture"])
    
    # 3. Contribuciones y takeaway (menor prioridad)
    if structured.get("takeaway"):
        high_value_terms.append(structured["takeaway"])
    for c in structured.get("contributions", [])[:5]:
        high_value_terms.append(c)
    
    # Procesar: extraer n-grams limpios de términos de alto valor
    clean_terms = []
    for term in high_value_terms:
        if not term or len(term.strip()) < 3:
            continue
        term = term.strip()
        
        # Si es corto (< 60 chars), usarlo tal cual (probablemente ya es un n-gram limpio)
        if len(term) <= 60:
            clean_terms.append(term)
        else:
            # Extraer n-grams técnicos de frases largas
            words = term.split()
            # 1-grams: palabras técnicas
            for w in words:
                w_clean = w.strip(".,;:()[]{}").lower()
                if len(w_clean) > 3 and w_clean not in ("the", "and", "for", "with", "using", "based", "model", "data", "method", "approach", "system", "analysis", "study", "results", "proposed", "novel", "framework"):
                    clean_terms.append(w_clean)
            # 2-grams y 3-grams técnicos
            for n in [2, 3]:
                for i in range(len(words) - n + 1):
                    ngram = " ".join(words[i:i+n]).strip(".,;:()[]{}").lower()
                    if len(ngram) > 5 and not all(w in ("the", "and", "for", "with", "using", "based", "model", "data", "proposed", "novel") for w in ngram.split()):
                        clean_terms.append(ngram)
    
    # También incluir términos cortos del filtered original (contribuciones, limitaciones, etc.)
    for term in filtered:
        if len(term) <= 60 and term not in clean_terms:
            clean_terms.append(term)
    
    # Deduplicar manteniendo orden de prioridad
    unique_terms = list(dict.fromkeys(clean_terms))
    return unique_terms[:80]  # Top 80


# 🔥 NUEVO: Cargar papers zonificados (argumentative zoning)
def load_zoned_papers(output_dir: Path) -> Dict[str, dict]:
    """Carga papers_zoned.json y retorna dict paper_id -> zoned_paper."""
    zoned_file = output_dir / "papers_zoned.json"
    if not zoned_file.exists():
        return {}
    try:
        with open(zoned_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {p.get("paper_id"): p for p in data if p.get("paper_id")}
    except Exception as e:
        writeLog("warning", logger, f"[ConceptMining] Could not load papers_zoned.json: {e}")
    return {}


def build_enriched_text(paper: dict, structured: dict = None, zoned: dict = None, max_section_chars: int = 8000) -> str:
    """Construye texto combinado incluyendo secciones estructuradas virtuales + zonas argumentativas."""
    parts = []
    
    # Texto original (clean_text + secciones)
    text = paper.get("clean_text", "")
    if text and len(text) > 500:
        parts.append(text[:max_section_chars * 2])
    sections = paper.get("clean_sections", {})
    for sec in ["methodology", "methods", "results", "findings", "conclusion", "introduction", "discussion"]:
        sec_text = sections.get(sec, "")
        if sec_text and len(sec_text) > 100:
            parts.append(sec_text[:max_section_chars])
    
    # 🔥 Añadir datos estructurados como secciones virtuales
    if structured and not structured.get("skipped"):
        # Contribuciones
        contribs = structured.get("contributions", [])
        if contribs:
            parts.append("CONTRIBUTIONS:\n" + "\n".join(f"- {c}" for c in contribs))
        
        # Metodología estructurada
        method = structured.get("methodology", {})
        if isinstance(method, dict):
            method_parts = []
            for k in ["approach", "architecture"]:
                if method.get(k):
                    method_parts.append(f"{k}: {method[k]}")
            if method.get("datasets"):
                method_parts.append(f"datasets: {', '.join(method['datasets'])}")
            if method.get("baselines"):
                method_parts.append(f"baselines: {', '.join(method['baselines'])}")
            if method.get("metrics"):
                method_parts.append(f"metrics: {', '.join(method['metrics'])}")
            if method.get("signal_modalities"):
                method_parts.append(f"signals: {', '.join(method['signal_modalities'])}")
            if method_parts:
                parts.append("METHODOLOGY_STRUCTURED:\n" + "\n".join(method_parts))
        
        # Resultados estructurados
        results = structured.get("results", {})
        if isinstance(results, dict):
            res_parts = []
            if results.get("main"):
                res_parts.append(f"main: {results['main']}")
            if results.get("ablation"):
                res_parts.append(f"ablation: {'; '.join(results['ablation'])}")
            if results.get("key_numbers"):
                res_parts.append(f"key_numbers: {'; '.join(results['key_numbers'])}")
            if res_parts:
                parts.append("RESULTS_STRUCTURED:\n" + "\n".join(res_parts))
        
        # Takeaway
        if structured.get("takeaway"):
            parts.append(f"TAKEAWAY: {structured['takeaway']}")
    
    # 🔥 Añadir párrafos de zonas prioritarias (claim, evidence, method)
    if zoned and zoned.get("paragraphs"):
        priority_zones = ["claim", "evidence", "method"]
        zoned_parts = []
        for p in zoned["paragraphs"]:
            if p.get("zone") in priority_zones and p.get("confidence", 0) > 0.3:
                zoned_parts.append(f"[{p['zone'].upper()}] {p['text'][:1500]}")
        if zoned_parts:
            parts.append("ARGUMENTATIVE_ZONES:\n" + "\n\n".join(zoned_parts))
    
    return "\n\n".join(parts)


# Lazy loading: model loaded on first call
_keybert_model: KeyBERT | None = None


def _get_keybert() -> KeyBERT:
    global _keybert_model
    if _keybert_model is None:
        # KeyBERT(model=...) acepta un nombre de SentenceTransformer; usar el
        # mismo espacio de embeddings que discovery/alignment/evidence/cluster.
        _keybert_model = KeyBERT(model=EMBEDDING_MODEL)
    return _keybert_model


def _get_semantic_model() -> SentenceTransformer:
    """Carga (perezosamente) un modelo SentenceTransformer para embeddings de deduplicación."""
    global _semantic_model
    if _semantic_model is None:
        writeLog("info", logger,
                 f"[ConceptMining] Cargando modelo de embeddings para dedup semántica: {EMBEDDING_MODEL}")
        _semantic_model = SentenceTransformer(EMBEDDING_MODEL)
    return _semantic_model


def deduplicate_semantic_terms(terms: list[str], threshold: float = None,
                               model: SentenceTransformer = None) -> list[str]:
    """
    Elimina términos semánticamente duplicados basándose en similitud coseno.

    Args:
        terms: Lista de términos (ya ordenados por prioridad: primero = más importante).
        threshold: Umbral de similitud coseno. Términos con similitud >= threshold
                   se consideran duplicados y se elimina el de menor prioridad.
        model: Modelo SentenceTransformer cargado. Si None, se carga por defecto.

    Returns:
        Lista de términos sin duplicados semánticos, manteniendo el orden de prioridad original.
    """
    if not terms or len(terms) <= 1:
        return terms

    if threshold is None:
        threshold = SEMANTIC_DEDUP_THRESHOLD

    if model is None:
        model = _get_semantic_model()

    # Calcular embeddings de todos los términos
    embeddings = model.encode(terms, normalize_embeddings=True, batch_size=32, show_progress_bar=False)

    # Matriz de similitud coseno (embeddings ya normalizados)
    sim_matrix = embeddings @ embeddings.T

    kept: list[str] = []
    kept_indices: list[int] = []
    n = len(terms)

    for i in range(n):
        if i in kept_indices:
            continue
        # Marcar este término como mantenido
        kept.append(terms[i])
        kept_indices.append(i)

        # Marcar como duplicados a todos los términos posteriores con similitud >= threshold
        for j in range(i + 1, n):
            if j in kept_indices:
                continue
            if float(sim_matrix[i][j]) >= threshold:
                kept_indices.append(j)  # marca como procesado/duplicado

    writeLog("info", logger,
             f"[ConceptMining] Dedup semántica: {n} términos → {len(kept)} "
             f"(umbral={threshold})")

    return kept


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


def build_document_objects(clean_corpus_dict: dict, doc_topic_map: list, target_topic: int, 
                           structured_map: Dict[str, dict] = None, zoned_map: Dict[str, dict] = None) -> list:
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
        structured = structured_map.get(doc_id) if structured_map else None
        zoned = zoned_map.get(doc_id) if zoned_map else None

        # 1) If section is known, extract that specific section
        if section_name:
            sections = paper.get("clean_sections", {}) or paper.get("sections", {})
            section_text = sections.get(section_name, "")
            if section_text and len(section_text) > 50:
                # Guardamos también el nombre de la sección para luego incluirlo en sections_used
                topic_docs_map[(doc_id, target_topic)].append((section_text, title, section_name, structured, zoned))
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
            topic_docs_map[(doc_id, target_topic)].append((text, title, "fallback", structured, zoned))

    # Build final list, one entry per (doc_id, topic) con texto combinado y secciones usadas
    results = []
    for (doc_id, topic), items in topic_docs_map.items():
        texts = []
        titles = []
        sections_used_set = set()
        structured_merged = None
        zoned_merged = None
        
        for item in items:
            texts.append(item[0])
            titles.append(item[1])
            if len(item) > 2:
                sec = item[2]
                if sec and sec != "fallback":
                    sections_used_set.add(sec)
            # Merge structured data (should be same for same doc_id)
            if len(item) > 3 and item[3]:
                structured_merged = item[3]
            # Merge zoned data
            if len(item) > 4 and item[4]:
                zoned_merged = item[4]
        
        # Fuego Usar texto enriquecido con datos estructurados + zonas argumentativas
        original_text = "\n\n".join(texts)
        combined_text = build_enriched_text(
            {"clean_text": original_text, "clean_sections": paper.get("clean_sections", {}) if (paper := clean_corpus_dict.get(doc_id)) else {}},
            structured_merged,
            zoned_merged
        )
        sections_used = sorted(list(sections_used_set))
        results.append({
            "doc_id": doc_id,
            "title": title,
            "text": combined_text,
            "sections_used": sections_used,
            "structured_terms": extract_structured_terms(structured_merged) if structured_merged else []
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
    Estrategia en tres pasos:
    1. Extracción libre sin candidates para capturar términos relevantes generales.
    2. Extracción con candidates=focus_terms + structured_terms para capturar términos del estudio y datos estructurados.
    3. INYECCIÓN DIRECTA de términos estructurados de alto valor (key_numbers, metrics, datasets, signals) al resultado final.
    
    Prioridad: injected_structured > candidate_terms (focus + structured) > free_terms
    """
    if not topic_docs:
        return []

    # Recopilar términos estructurados de todos los documentos del tópico
    all_structured_terms = []
    for doc in topic_docs:
        all_structured_terms.extend(doc.get("structured_terms", []))
    
    # Extraer términos de alto valor directamente inyectables (no dependen de KeyBERT matching)
    # Estos son: key_numbers, metrics, datasets, signal_modalities, baselines, tables
    direct_inject_terms = set()
    for term in all_structured_terms:
        term_clean = term.strip()
        # Priorizar términos que parecen métricas, datasets, signals, baselines, tablas
        term_lower = term_clean.lower()
        if (any(ind in term_lower for ind in ["%", "accuracy", "f1", "auc", "precision", "recall", "sensitivity", "specificity", 
                                              "r=", "p=", "kappa", "variance", "cohen",
                                              "table", "figure", "dataset", "baseline",
                                              "eeg", "ecg", "emg", "hrv", "ppg", "accelerometer", "signal",
                                              "conmil", "ssr", "vlm", "transformer", "bert", "gpt", "llm"]) 
            and len(term_clean) > 3):
            # Limpiar y normalizar
            cleaned = term_clean.strip(".,;:()[]{}")
            if len(cleaned) > 3:
                direct_inject_terms.add(cleaned)
    
    # Limitar términos inyectados directamente
    direct_inject = list(direct_inject_terms)[:15]  # Top 15 términos inyectados

    # Recopilar structured_terms para candidates de KeyBERT
    structured_terms = list(set(all_structured_terms))[:50]
    
    # Combinar focus_terms + structured_terms como candidatos
    all_candidates = list(focus_terms or []) + list(structured_terms)
    # Deduplicar manteniendo orden (focus_terms primero)
    seen = set()
    unique_candidates = []
    for c in all_candidates:
        if c not in seen:
            seen.add(c)
            unique_candidates.append(c)

    # Texto combinado
    weighted_texts = []
    for doc in topic_docs:
        text = doc.get("text", "")
        if text:
            weighted_texts.append(text)

    combined_text = "\n".join(weighted_texts)
    if not combined_text.strip():
        # Si no hay texto, devolver solo términos inyectados
        return direct_inject[:TOP_K_KEYWORDS]

    kw_model = _get_keybert()

    # Paso 1: extracción libre sin candidates
    free_keywords = kw_model.extract_keywords(
        combined_text,
        keyphrase_ngram_range=NGRAM_RANGE,
        stop_words="english",
        top_n=TOP_K_KEYWORDS
    )
    free_terms = set([kw for kw, _ in free_keywords])

    # Paso 2: extracción con candidates (focus_terms + structured_terms)
    candidate_terms = set()
    if unique_candidates:
        candidate_keywords = kw_model.extract_keywords(
            combined_text,
            keyphrase_ngram_range=NGRAM_RANGE,
            stop_words="english",
            top_n=min(len(unique_candidates), TOP_K_KEYWORDS),
            candidates=unique_candidates
        )
        candidate_terms = set([kw for kw, _ in candidate_keywords])

    # Combinar con PRIORIDAD:
    # 1. Términos inyectados directamente (key_numbers, metrics, datasets, signals)
    # 2. Términos candidatos (focus + structured) que KeyBERT encontró
    # 3. Términos libres
    final_terms = []
    
    # Prioridad 1: Inyección directa
    for t in direct_inject:
        if t not in final_terms:
            final_terms.append(t)
    
    # Prioridad 2: Candidatos encontrados por KeyBERT
    for t in candidate_terms:
        if t not in final_terms:
            final_terms.append(t)
    
    # Prioridad 3: Términos libres
    for t in free_terms:
        if t not in final_terms:
            final_terms.append(t)
    
    final_terms = final_terms[:TOP_K_KEYWORDS]

    # Deduplicación semántica: eliminar términos con similitud coseno >= umbral
    if SEMANTIC_DEDUP_ENABLED and len(final_terms) > 1:
        try:
            sem_model = _get_semantic_model()
            final_terms = deduplicate_semantic_terms(
                final_terms,
                threshold=SEMANTIC_DEDUP_THRESHOLD,
                model=sem_model,
            )
        except Exception as exc:
            writeLog("warning", logger,
                     f"[ConceptMining] Dedup semántica falló: {exc}. "
                     f"Devolviendo términos sin dedup semántica.")

    return final_terms


# 🔥 NUEVO: Ahora recibe focus_terms
def build_concept_candidates(
        topic_data: dict,
        papers_text: list,
        papers_metadata: list,
        focus_terms: list[str] = None,
        output_dir: Path = None
) -> list:
    """
    Builds concept candidates from:
    - topic_data (candidate_concepts.json)
    - papers_text (papers_text.json) containing clean_text and clean_sections
    - papers_metadata (papers_metadata.json) containing title and other metadata
    - focus_terms (optional) to guide keyword extraction
    - output_dir (optional) to load papers_structured.json
    """
    # 🔥 Cargar papers estructurados
    structured_map = load_structured_papers(output_dir) if output_dir else {}
    
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

        docs_for_topic = build_document_objects(corpus_dict, doc_topic_map, topic_id, structured_map)
        keywords = extract_topic_keywords(docs_for_topic, focus_terms)

        writeLog(
            "info", logger,
            f"[ConceptMining] topic {topic_id} '{topic_label[:50]}' "
            f"→ {len(docs_for_topic)} documents, {len(keywords)} keywords"
        )

        concepts.append({
            "topic_id": topic_id,
            "topic_label": topic_label,
            "study_similarity": topic.get("study_similarity"),
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

    candidates = build_concept_candidates(topic_data, papers_text, papers_metadata, focus_terms, output_dir)
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