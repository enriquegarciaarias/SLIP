# clusterEvidences.py
from sources.common.common import logger, writeLog, processControl
from sources.common.utils import inicioModulo

import json
import numpy as np
from pathlib import Path
from sentence_transformers import SentenceTransformer
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer
from collections import defaultdict

# clusterEvidences.py
from sources.common.common import logger, writeLog, processControl
from sources.common.utils import inicioModulo

import json
import numpy as np
from pathlib import Path
from sentence_transformers import SentenceTransformer
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer
from collections import defaultdict

# --------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------
EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"

# Umbrales de distancia coseno por número de evidencias.
# AgglomerativeClustering con metric=cosine trabaja en espacio de DISTANCIA
# (0=idénticos). Con vectores normalizados: distancia = 1 - similitud_coseno.
#
# REVISIÓN v3: Los valores anteriores (0.26 para n>=30) combinados con el
# ajuste por focus_density producían demasiados singletons (ej: 54 evidencias
# → 31 clusters).
#
# Nuevo enfoque: umbrales más permisivos que buscan clusters significativos
# (3+ evidencias), complementados con fusión post-clustering de singletons
# que están temáticamente cerca de un cluster más grande.
CLUSTER_THRESHOLDS = [
    (40, 0.38),  # >=40 evidencias: umbral moderado (min_similarity=0.62)
    (30, 0.36),  # >=30 evidencias: (era 0.26 → ahora 0.36, mucho más permisivo)
    (20, 0.34),  # >=20 evidencias: nuevo rango intermedio
    (15, 0.32),  # >=15 evidencias: (era 0.28 → 0.32)
    (10, 0.35),  # >=10 evidencias: (sin cambio)
    (6, 0.38),  # >=6  evidencias: (era 0.35 → 0.38, ligeramente más permisivo)
    (3, 0.42),  # >=3 evidencias: (era 0.40 → 0.42)
    (0, 0.48),  # <3   evidencias: (era 0.45 → 0.48)
]

# --- AJUSTE POR FOCUS_DENSITY: DESACTIVADO ---
# HISTORIAL DE ESTE PARÁMETRO:
# v1: Aplicaba RELAJACIÓN con alta densidad (erróneo: agrupaba dominios distintos)
# v2: Aplicaba RESTRICCIÓN con alta densidad (erróneo: producía muchos singletons)
# v3: DESACTIVADO. Cuando focus_density es alta (ej: 0.80), significa que el
#     concepto es coherente y casi todos los textos son relevantes. El threshold
#     base ya debería manejar esto correctamente. Ajustar adicionalmente solo
#     complicaba la calibración sin beneficio real.
FOCUS_DENSITY_ADJUSTMENT = False

# --- FUSIÓN DE SINGLETONS (NUEVO) ---
# Después del clustering aglomerativo, los clusters de tamaño 1 que están
# "cerca" de un cluster más grande se fusionan con él. Esto reduce el
# ruido de singletons que no aportan valor sintético.
MERGE_SINGLETONS = True
MERGE_DISTANCE_THRESHOLD = 0.50  # Distancia máxima para fusionar (sim >= 0.50)

# --- Configuración de Generación de Temas con LLM ---
LLM_THEME_GENERATION = True  # Activar generación de temas con LLM
MAX_THEME_KEYWORDS = 8  # Máximo de keywords extraídas por TF-IDF para pasar al LLM
THEME_MAX_TOKENS = 200  # ~120-150 palabras (3-4 frases descriptivas)


def load_cluster_evidences_config() -> None:
    """
    Sobrescribe las constantes del módulo con la sección "clusterEvidences"
    de config.json (processControl.defaults). Los valores del fichero tienen
    prioridad; si faltan, se conservan los valores por defecto.
    """
    defaults = getattr(processControl, "defaults", None) or {}
    cfg = defaults.get("clusterEvidences", {}) if isinstance(defaults, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}

    global EMBEDDING_MODEL, CLUSTER_THRESHOLDS, FOCUS_DENSITY_ADJUSTMENT, \
        MERGE_SINGLETONS, MERGE_DISTANCE_THRESHOLD, LLM_THEME_GENERATION, \
        MAX_THEME_KEYWORDS, THEME_MAX_TOKENS

    EMBEDDING_MODEL = cfg.get("embedding_model", EMBEDDING_MODEL)
    thresholds = cfg.get("cluster_thresholds", CLUSTER_THRESHOLDS)
    if isinstance(thresholds, list):
        CLUSTER_THRESHOLDS = [tuple(t) for t in thresholds if isinstance(t, (list, tuple))]
    FOCUS_DENSITY_ADJUSTMENT = bool(cfg.get("focus_density_adjustment", FOCUS_DENSITY_ADJUSTMENT))
    MERGE_SINGLETONS = bool(cfg.get("merge_singletons", MERGE_SINGLETONS))
    MERGE_DISTANCE_THRESHOLD = float(cfg.get("merge_distance_threshold", MERGE_DISTANCE_THRESHOLD))
    LLM_THEME_GENERATION = bool(cfg.get("llm_theme_generation", LLM_THEME_GENERATION))
    MAX_THEME_KEYWORDS = int(cfg.get("max_theme_keywords", MAX_THEME_KEYWORDS))
    THEME_MAX_TOKENS = int(cfg.get("theme_max_tokens", THEME_MAX_TOKENS))


# --------------------------------------------------
# FUNCIONES AUXILIARES
# --------------------------------------------------

def load_focus_terms(input_dir: Path) -> list[str]:
    json_file = input_dir / "studyDescription.json"
    if json_file.exists():
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("project", {}).get("focus_terms", [])
        except Exception as e:
            writeLog("error", logger, f"[Cluster] Error loading focus_terms: {e}")
    return []


def load_evidence(input_file: Path) -> dict:
    with open(input_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def extract_evidence_texts(concept_data: dict) -> tuple[list, list]:
    texts = []
    metadata = []
    for unit in concept_data.get("knowledge_units", []):
        texts.append(unit["evidence_text"])
        metadata.append({
            "doc_id": unit["doc_id"],
            "paper_title": unit["paper_title"],
            "similarity": unit["similarity"]
        })
    return texts, metadata


# --------------------------------------------------
# PER-CONCEPT FOCUS DENSITY
# --------------------------------------------------

def compute_focus_density(texts: list[str], focus_terms: list[str]) -> float:
    """
    Retorna la fracción de textos que contienen al menos un término de foco.
    Calculado POR CONCEPTO, usando sus propias evidencias.

    NOTA: Este valor se conserva en la salida para análisis, pero ya NO
    modifica el threshold de clustering (ver FOCUS_DENSITY_ADJUSTMENT).
    """
    if not texts or not focus_terms:
        return 0.0

    focus_lower = [t.lower() for t in focus_terms]
    hits = sum(
        1 for text in texts
        if any(term in text.lower() for term in focus_lower)
    )
    return hits / len(texts)


def get_distance_threshold(
        n: int,
        focus_density: float = 0.0,
) -> float:
    """
    Calcula el umbral de distancia coseno para AgglomerativeClustering.

    v3 SIMPLIFICADO: Solo aplica el ajuste por volumen (CLUSTER_THRESHOLDS).
    El ajuste por focus_density ha sido desactivado porque:
    - Con densidad alta, reducía demasiado el umbral → muchos singletons
    - La coherencia temática ya viene dada por la query del concepto
    - El threshold base es suficiente para distinguir subtemas

    El parámetro focus_density se mantiene en la firma por compatibilidad
    y se usa solo para logging informativo.
    """
    base_threshold = 0.48
    for min_n, threshold in CLUSTER_THRESHOLDS:
        if n >= min_n:
            base_threshold = threshold
            break

    # Logging informativo (el ajuste está desactivado)
    if focus_density >= 0.50:
        writeLog("info", logger,
                 f"[Clustering] focus_density={focus_density:.2f} (alto, pero sin ajuste) "
                 f"→ threshold={base_threshold:.3f}")

    return base_threshold


# --------------------------------------------------
# FUSIÓN DE SINGLETONS (NUEVO)
# --------------------------------------------------

def merge_small_clusters(
        clusters: dict,
        embeddings: np.ndarray,
        distance_threshold: float = 0.50
) -> dict:
    """
    Fusiona clusters de tamaño 1 con el cluster más cercano si la distancia
    promedio está por debajo del umbral.

    Estrategia:
    1. Identificar singletons y clusters grandes (size >= 2)
    2. Para cada singleton, calcular distancia promedio a cada cluster grande
    3. Si el mínimo está por debajo del umbral, fusionar
    4. Los singletons que no se fusionan se mantienen (son temáticamente únicos)

    Args:
        clusters: dict {label: {"texts": [], "metadata": [], "scores": []}}
        embeddings: np.ndarray con los embeddings originales (normalizados)
        distance_threshold: distancia máxima para fusionar

    Returns:
        dict con clusters fusionados, labels reasignados consecutivamente
        por tamaño descendente
    """
    if not MERGE_SINGLETONS:
        return clusters

    # Separar singletons de clusters grandes
    singletons = {}
    large_clusters = {}

    for label, data in clusters.items():
        if data["size"] == 1:
            singletons[label] = data
        else:
            large_clusters[label] = data

    # Si no hay singletons O no hay clusters grandes, nada que fusionar
    if not singletons or not large_clusters:
        return clusters

    # Construir índice: texto → posición en el array de embeddings
    text_to_idx = {}
    idx = 0
    for label, data in clusters.items():
        for text in data["texts"]:
            text_to_idx[text] = idx
            idx += 1

    # Pre-computar centroides de clusters grandes
    large_centroids = {}
    for label, data in large_clusters.items():
        indices = [text_to_idx[t] for t in data["texts"] if t in text_to_idx]
        if indices:
            centroid = np.mean(embeddings[indices], axis=0)
            # Normalizar el centroide para distancias coseno consistentes
            norm = np.linalg.norm(centroid)
            if norm > 0:
                centroid = centroid / norm
            large_centroids[label] = centroid

    merged = {k: dict(v) for k, v in large_clusters.items()}  # Copia superficial
    merged_singletons = 0
    singleton_details = []

    for s_label, s_data in singletons.items():
        s_text = s_data["texts"][0]
        s_idx = text_to_idx.get(s_text)

        if s_idx is None:
            merged[s_label] = s_data
            continue

        s_embedding = embeddings[s_idx]

        # Encontrar el cluster grande más cercano (por centroide)
        best_label = None
        best_distance = float('inf')

        for l_label, l_centroid in large_centroids.items():
            # Distancia coseno = 1 - similitud (embeddings normalizados)
            dist = 1 - np.dot(s_embedding, l_centroid)
            if dist < best_distance:
                best_distance = dist
                best_label = l_label

        # Fusionar si está lo suficientemente cerca
        if best_label is not None and best_distance <= distance_threshold:
            merged[best_label]["texts"].append(s_data["texts"][0])
            merged[best_label]["metadata"].append(s_data["metadata"][0])
            merged[best_label]["scores"].append(s_data["scores"][0])
            merged[best_label]["size"] = len(merged[best_label]["texts"])
            merged_singletons += 1
            singleton_details.append((best_label, best_distance))
        else:
            merged[s_label] = s_data

    if merged_singletons > 0:
        writeLog("info", logger,
                 f"[Merge] {merged_singletons}/{len(singletons)} singletons fusionados "
                 f"(distancias: {[f'{d:.3f}' for _, d in singleton_details]})")

    # Reasignar labels consecutivamente, ordenados por tamaño descendente
    final_clusters = {}
    sorted_items = sorted(merged.items(), key=lambda x: -x[1]["size"])
    for new_label, (old_label, data) in enumerate(sorted_items):
        final_clusters[new_label] = data

    return final_clusters


# --------------------------------------------------
# CLUSTERING CORE
# --------------------------------------------------

def cluster_evidences(
        texts: list,
        metadata: list,
        model: SentenceTransformer,
        focus_density: float = 0.0,
) -> dict:
    """
    Clustering principal de evidencias.

    Flujo:
    1. Generar embeddings con SentenceTransformer
    2. Aplicar AgglomerativeClustering con threshold dinámico
    3. Fusionar singletons cercanos (si MERGE_SINGLETONS=True)
    4. Retornar estructura con estadísticas por cluster
    """
    if len(texts) == 0:
        return {"clusters": {}}

    if len(texts) == 1:
        scores = [metadata[0]["similarity"]] if metadata else []
        return {
            "clusters": {
                0: {
                    "texts": texts,
                    "metadata": metadata,
                    "size": len(texts),
                    "scores": scores
                }
            }
        }

    embeddings = model.encode(
        texts,
        normalize_embeddings=True,
        batch_size=32,
        show_progress_bar=False
    )

    n = len(texts)
    distance_threshold = get_distance_threshold(n, focus_density)
    writeLog("info", logger,
             f"[Clustering] {n} evidencias, threshold={distance_threshold:.3f} "
             f"(focus_density={focus_density:.2f}, min_similarity={1 - distance_threshold:.2f})")

    clustering = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=distance_threshold,
        metric='cosine',
        linkage='average'
    )
    labels = clustering.fit_predict(embeddings)

    # Agrupar por label
    clusters = defaultdict(lambda: {"texts": [], "metadata": [], "scores": []})
    for idx, label in enumerate(labels):
        clusters[label]["texts"].append(texts[idx])
        clusters[label]["metadata"].append(metadata[idx])
        clusters[label]["scores"].append(metadata[idx]["similarity"])

    # Calcular tamaños
    for label in clusters:
        clusters[label]["size"] = len(clusters[label]["texts"])

    # NUEVO: Fusionar singletons cercanos a clusters grandes
    initial_n_clusters = len(clusters)
    initial_singletons = sum(1 for c in clusters.values() if c["size"] == 1)

    clusters = merge_small_clusters(clusters, embeddings, MERGE_DISTANCE_THRESHOLD)

    final_n_clusters = len(clusters)
    final_singletons = sum(1 for c in clusters.values() if c["size"] == 1)

    if initial_n_clusters != final_n_clusters:
        writeLog("info", logger,
                 f"[Clustering] Post-merge: {initial_n_clusters}→{final_n_clusters} clusters, "
                 f"singletons: {initial_singletons}→{final_singletons}")

    # Construir resultado
    result = {
        "n_clusters": len(clusters),
        "clusters": {}
    }
    for label, data in clusters.items():
        result["clusters"][int(label)] = {
            "size": len(data["texts"]),
            "avg_similarity": round(np.mean(data["scores"]), 4) if data["scores"] else 0,
            "max_similarity": round(max(data["scores"]), 4) if data["scores"] else 0,
            "min_similarity": round(min(data["scores"]), 4) if data["scores"] else 0,
            "texts": data["texts"],
            "metadata": data["metadata"],
            "scores": data["scores"]
        }
    return result


# --------------------------------------------------
# GENERACIÓN DE TEMAS CON LLM (TF-IDF + LLM)
# --------------------------------------------------

def _generate_llm_theme(keywords: list[str], concept_query: str, llm_client=None) -> str:
    """
    Usa el LLM para generar un párrafo temático descriptivo (3-4 frases)
    a partir de las palabras clave compartidas y la pregunta de investigación.
    """
    if not keywords:
        return "No evidence provided."

    try:
        from sources.common.llm_client import create_resilient_ollama_client

        if llm_client is None:
            llm_client = create_resilient_ollama_client()

        prompt = (
            "You are an expert systematic literature reviewer. "
            "Given a list of key concepts extracted from a group of academic papers, "
            "and the overarching research question they belong to, write a descriptive "
            "thematic summary (approx. 100-130 words, 3 to 4 sentences) that captures "
            "the core shared focus of this specific cluster in the context of the research question.\n\n"
            f"Research Question: {concept_query}\n"
            f"Key extracted concepts: {', '.join(keywords)}\n\n"
            "Thematic Summary:"
        )

        theme = llm_client.generate_text(
            prompt=prompt,
            temperature=0.15,  # Baja para mantenerse fiel a las keywords
            max_tokens=THEME_MAX_TOKENS,
            context="cluster-theme-generation"
        )

        if theme and len(theme) > 50:
            theme = theme.strip().rstrip('.')
            # Asegurar que empiece con mayúscula
            if theme and theme[0].islower():
                theme = theme[0].upper() + theme[1:]
            return theme

    except Exception as e:
        writeLog("warning", logger, f"[Cluster] LLM theme generation failed: {e}")

    # Fallback si el LLM falla o alucina
    if keywords:
        return "Focus on: " + ", ".join(keywords) + "."

    return "No evidence provided."


def _generate_llm_theme_from_texts(texts: list[str], concept_query: str, llm_client=None) -> str:
    """
    Extrae palabras clave compartidas usando TF-IDF rápido y luego
    le pide al LLM que genere un párrafo temático en contexto.
    """
    try:
        vectorizer = TfidfVectorizer(
            ngram_range=(1, 3),
            max_features=1000,
            stop_words='english',
            min_df=1,
        )

        tfidf_matrix = vectorizer.fit_transform(texts)
        feature_names = vectorizer.get_feature_names_out()
        mean_tfidf = np.asarray(tfidf_matrix.mean(axis=0)).flatten()

        # Obtener el Top N de conceptos más representativos compartidos
        top_indices = mean_tfidf.argsort()[-MAX_THEME_KEYWORDS:][::-1]
        top_keywords = [feature_names[i] for i in top_indices if mean_tfidf[i] > 0]

        if top_keywords:
            return _generate_llm_theme(top_keywords, concept_query, llm_client)

    except Exception as e:
        writeLog("debug", logger, f"[Cluster] TF-IDF extraction failed: {e}")

    # Fallback extremo si falla el TF-IDF
    return texts[0][:600] if texts else "No evidence"


# --------------------------------------------------
# ENSAMBLADO DE SALIDA
# --------------------------------------------------

def generate_cluster_summary(
        cluster_data: dict,
        concept_id: int,
        concept_query: str,
        focus_terms: list[str] = None,
        focus_density: float = 0.0,
        llm_client=None
) -> dict:
    summary = {
        "concept_id": concept_id,
        "concept_query": concept_query,
        "focus_terms_used": focus_terms or [],
        "focus_density": round(focus_density, 4),
        "total_evidences": sum(c["size"] for c in cluster_data["clusters"].values()),
        "n_clusters": cluster_data.get("n_clusters", len(cluster_data["clusters"])),
        "clusters": []
    }

    focus_lower = [t.lower() for t in (focus_terms or [])]

    for label, data in cluster_data["clusters"].items():
        unique_papers = {}
        for meta in data["metadata"]:
            unique_papers[meta["doc_id"]] = meta["paper_title"]

        scores = data.get("scores", [])
        texts = data["texts"]

        # Lógica del tema sugerido: LLM para clusters con 2+ textos, truncamiento para singletons
        if LLM_THEME_GENERATION and len(texts) > 1:
            suggested_theme = _generate_llm_theme_from_texts(texts, concept_query, llm_client)
        else:
            suggested_theme = texts[0][:600] if texts else "No evidence"

        focus_rich_count = 0
        for text in texts:
            lower_text = text.lower()
            if any(term in lower_text for term in focus_lower):
                focus_rich_count += 1

        cluster_info = {
            "cluster_id": int(label),
            "size": data["size"],
            "avg_evidence_score": round(np.mean(scores), 4) if scores else 0.0,
            "focus_rich_count": focus_rich_count,
            "focus_rich_percentage": round((focus_rich_count / data["size"]) * 100, 1) if data["size"] > 0 else 0,
            "suggested_theme": suggested_theme,
            "papers": list(unique_papers.keys()),
            "paper_titles": list(unique_papers.values()),
            "evidences": [
                {
                    "text": t,
                    "doc_id": m["doc_id"],
                    "similarity": m["similarity"]
                }
                for t, m in zip(data["texts"], data["metadata"])
            ]
        }
        summary["clusters"].append(cluster_info)

    summary["clusters"].sort(key=lambda x: x["size"], reverse=True)
    return summary


# --------------------------------------------------
# REPORTES
# --------------------------------------------------

def print_cluster_report(summary: dict):
    """Mantiene el reporte decorativo en el log de producción (por decisión del usuario)."""
    writeLog("info", logger, f"\n{'=' * 60}")
    writeLog("info", logger, f"CONCEPTO {summary['concept_id']}: {summary['concept_query']}")
    if summary.get('focus_terms_used'):
        writeLog("info", logger, f"Focus terms: {', '.join(summary['focus_terms_used'][:10])}")
        writeLog("info", logger, f"Focus density: {summary.get('focus_density', 0):.2f}")
    writeLog("info", logger, f"{'=' * 60}")
    writeLog("info", logger, f"Total evidencias: {summary['total_evidences']}")
    writeLog("info", logger, f"Número de clusters: {summary['n_clusters']}")

    # NUEVO: Mostrar alerta si hay muchos singletons
    sizes = [c['size'] for c in summary["clusters"]]
    n_singletons = sum(1 for s in sizes if s == 1)
    if n_singletons > 0:
        pct = 100 * n_singletons / len(sizes) if sizes else 0
        writeLog("info", logger, f"⚠️  Singletons: {n_singletons}/{len(sizes)} ({pct:.0f}%)")

    for cluster in summary["clusters"]:
        size_marker = " [SINGLETON]" if cluster['size'] == 1 else ""
        writeLog("info", logger, f"\n--- CLUSTER {cluster['cluster_id']} (tamaño: {cluster['size']}){size_marker} ---")
        writeLog("info", logger, f"Score promedio: {cluster['avg_evidence_score']}")
        if cluster.get('focus_rich_percentage', 0) > 0:
            writeLog("info", logger,
                     f"Evidencias con focus terms: {cluster['focus_rich_count']} ({cluster['focus_rich_percentage']}%)")
        writeLog("info", logger, f"Tema sugerido: {cluster['suggested_theme']}...")
        writeLog("info", logger, f"Papers: {cluster['papers']}")
        for i, ev in enumerate(cluster["evidences"][:3]):
            writeLog("info", logger, f"  {i + 1}. [{ev['similarity']}] {ev['text'][:120]}...")
        if len(cluster["evidences"]) > 3:
            writeLog("info", logger, f"  ... y {len(cluster['evidences']) - 3} más")


# --------------------------------------------------
# ENTRY POINT
# --------------------------------------------------

def processClusterEvidences():
    load_cluster_evidences_config()
    input_dir, output_dir = inicioModulo("processConceptEvidence")
    input_file = output_dir / "concept_evidence.json"

    if not input_file.exists():
        writeLog("error", logger, f"[ClusterEvidences] No se encuentra {input_file}")
        return

    focus_terms = load_focus_terms(input_dir)
    if focus_terms:
        writeLog("info", logger, f"[ClusterEvidences] Loaded {len(focus_terms)} focus terms")
    else:
        writeLog("info", logger, "[ClusterEvidences] No focus terms found – using standard clustering")

    data = load_evidence(input_file)

    total_units = sum(len(c.get("knowledge_units", [])) for c in data.get("concepts", []))
    if total_units == 0:
        writeLog("info", logger, "[ClusterEvidences] No hay evidencias para clusterizar")
        output_file = output_dir / "clustered_evidences.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        return

    writeLog("info", logger, "Cargando modelo de embeddings...")
    model = SentenceTransformer(EMBEDDING_MODEL)

    # Crear un único cliente LLM para el módulo (reutilizar conexiones)
    llm_client = None
    if LLM_THEME_GENERATION:
        from sources.common.llm_client import create_resilient_ollama_client
        llm_client = create_resilient_ollama_client()

    model_evaluations = []

    for concept in data.get("concepts", []):
        concept_id = concept["concept_id"]
        concept_query = concept["concept_query"]
        knowledge_units = concept.get("knowledge_units", [])

        if len(knowledge_units) == 0:
            writeLog("info", logger, f"Concepto {concept_id}: Sin evidencias")
            model_evaluations.append({
                "concept_id": concept_id,
                "concept_query": concept_query,
                "focus_terms_used": focus_terms or [],
                "focus_density": 0.0,
                "total_evidences": 0,
                "n_clusters": 0,
                "clusters": [],
                "consensus": concept.get("consensus", []),
            })
            continue

        texts, metadata = extract_evidence_texts(concept)

        # Calcular densidad de focus_terms para ESTE concepto concreto
        # (se usa para logging y para la salida JSON, pero NO modifica el threshold)
        focus_density = compute_focus_density(texts, focus_terms)

        writeLog("info", logger,
                 f"Procesando Concepto {concept_id} ({len(knowledge_units)} evidencias, "
                 f"focus_density={focus_density:.2f})...")

        cluster_result = cluster_evidences(texts, metadata, model, focus_density)

        # Inyectar el cliente LLM para los temas
        summary = generate_cluster_summary(
            cluster_result, concept_id, concept_query, focus_terms, focus_density,
            llm_client=llm_client
        )
        summary["consensus"] = concept.get("consensus", [])
        model_evaluations.append(summary)

        # Reporte decorativo (se mantiene en el log, como el usuario prefiere)
        print_cluster_report(summary)

    # Guardar resultados JSON
    output_file = output_dir / "clustered_evidences.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(model_evaluations, f, indent=2, ensure_ascii=False)
    writeLog("info", logger, f"[ClusterEvidences] Guardado en {output_file}")

    # Resumen estadístico final (con métricas de singletons)
    writeLog("info", logger, "\n" + "=" * 80)
    writeLog("info", logger, "📊 RESUMEN ESTADÍSTICO DE CLUSTERING")
    writeLog("info", logger, "=" * 80)

    total_clusters = 0
    total_evidences = 0
    total_singletons = 0
    paper_concept_assignments = 0  # Un paper en 3 conceptos cuenta 3 veces
    all_unique_papers = set()  # Conjunto único real

    for summary in model_evaluations:
        concept_id = summary["concept_id"]
        concept_query = summary["concept_query"]
        n_clusters = len(summary["clusters"])
        n_evidences = summary["total_evidences"]
        concept_papers = set()
        concept_singletons = 0

        for cluster in summary["clusters"]:
            concept_papers.update(cluster["papers"])
            if cluster["size"] == 1:
                concept_singletons += 1

        all_unique_papers.update(concept_papers)

        total_clusters += n_clusters
        total_evidences += n_evidences
        total_singletons += concept_singletons
        paper_concept_assignments += len(concept_papers)

        query_short = concept_query[:70] + "..." if len(concept_query) > 70 else concept_query
        singleton_str = f", {concept_singletons} singletons" if concept_singletons > 0 else ""

        writeLog("info", logger,
                 f"Concepto {concept_id} [density={summary.get('focus_density', 0):.2f}]: "
                 f"{query_short} — {n_evidences} ev, {n_clusters} clust{singleton_str}, "
                 f"{len(concept_papers)} papers"
                 )

    writeLog("info", logger, "-" * 80)
    writeLog("info", logger, f"Total evidencias procesadas: {total_evidences}")
    writeLog("info", logger, f"Total clusters formados: {total_clusters}")
    if total_singletons > 0:
        pct = 100 * total_singletons / total_clusters if total_clusters > 0 else 0
        writeLog("info", logger, f"Total singletons: {total_singletons} ({pct:.1f}% del total)")
    else:
        writeLog("info", logger, "Total singletons: 0 ✓")
    writeLog("info", logger,
             f"Asignaciones paper-concepto: {paper_concept_assignments} "
             f"(un mismo paper puede contar en varios conceptos)")
    writeLog("info", logger, f"Papers únicos en el proyecto: {len(all_unique_papers)}")
    writeLog("info", logger, "=" * 80)


if __name__ == "__main__":
    processClusterEvidences()