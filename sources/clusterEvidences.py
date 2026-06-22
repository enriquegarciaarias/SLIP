# clusterEvidences.py
from sources.common.common import logger, writeLog, processControl
from sources.common.utils import inicioModulo

import json
import numpy as np
from pathlib import Path
from sentence_transformers import SentenceTransformer
from sklearn.cluster import AgglomerativeClustering
from collections import defaultdict

# --------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------
EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"

CLUSTER_THRESHOLDS = [
    (10, 0.30),
    (6, 0.35),
    (3, 0.40),
    (0, 0.45),
]

FOCUS_TERM_THRESHOLD_FACTOR = 0.95
FOCUS_DENSITY_THRESHOLD = 0.30  # Mínimo de evidencias con focus_terms para aplicar relajación


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
    n:             int,
    focus_density: float = 0.0,
) -> float:
    """
    Obtiene el umbral de distancia para clustering.
    El factor de relajación (0.95x) se aplica SOLO si focus_density >= FOCUS_DENSITY_THRESHOLD.
    """
    base_threshold = 0.45
    for min_n, threshold in CLUSTER_THRESHOLDS:
        if n >= min_n:
            base_threshold = threshold
            break

    if focus_density >= FOCUS_DENSITY_THRESHOLD:
        return base_threshold * FOCUS_TERM_THRESHOLD_FACTOR

    return base_threshold


def cluster_evidences(
    texts:         list,
    metadata:      list,
    model:         SentenceTransformer,
    focus_density: float = 0.0,
) -> dict:
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
        f"(focus_density={focus_density:.2f})")

    clustering = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=distance_threshold,
        metric='cosine',
        linkage='average'
    )
    labels = clustering.fit_predict(embeddings)

    clusters = defaultdict(lambda: {"texts": [], "metadata": [], "scores": []})
    for idx, label in enumerate(labels):
        clusters[label]["texts"].append(texts[idx])
        clusters[label]["metadata"].append(metadata[idx])
        clusters[label]["scores"].append(metadata[idx]["similarity"])

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


def generate_cluster_summary(
    cluster_data:  dict,
    concept_id:    int,
    concept_query: str,
    focus_terms:   list[str] = None,
    focus_density: float = 0.0,
) -> dict:
    summary = {
        "concept_id": concept_id,
        "concept_query": concept_query,
        "focus_terms_used": focus_terms or [],
        "focus_density": round(focus_density, 4),   # NUEVO: métrica por concepto
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

        best_idx = 0
        best_score = -1
        for idx, (text, score) in enumerate(zip(texts, scores)):
            lower_text = text.lower()
            focus_hits = sum(1 for term in focus_lower if term in lower_text)
            weighted_score = score + (0.05 * focus_hits)
            if weighted_score > best_score:
                best_score = weighted_score
                best_idx = idx

        suggested_theme = texts[best_idx][:150] if texts else "No evidence"

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

    for cluster in summary["clusters"]:
        writeLog("info", logger, f"\n--- CLUSTER {cluster['cluster_id']} (tamaño: {cluster['size']}) ---")
        writeLog("info", logger, f"Score promedio: {cluster['avg_evidence_score']}")
        if cluster.get('focus_rich_percentage', 0) > 0:
            writeLog("info", logger, f"Evidencias con focus terms: {cluster['focus_rich_count']} ({cluster['focus_rich_percentage']}%)")
        writeLog("info", logger, f"Tema sugerido: {cluster['suggested_theme']}...")
        writeLog("info", logger, f"Papers: {cluster['papers']}")
        for i, ev in enumerate(cluster["evidences"][:3]):
            writeLog("info", logger, f"  {i + 1}. [{ev['similarity']}] {ev['text'][:120]}...")
        if len(cluster["evidences"]) > 3:
            writeLog("info", logger, f"  ... y {len(cluster['evidences']) - 3} más")


def processClusterEvidences():
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
                "clusters": []
            })
            continue

        texts, metadata = extract_evidence_texts(concept)

        # Calcular densidad de focus_terms para ESTE concepto concreto
        focus_density = compute_focus_density(texts, focus_terms)

        writeLog("info", logger,
            f"Procesando Concepto {concept_id} ({len(knowledge_units)} evidencias, "
            f"focus_density={focus_density:.2f})...")

        cluster_result = cluster_evidences(texts, metadata, model, focus_density)
        summary = generate_cluster_summary(
            cluster_result, concept_id, concept_query, focus_terms, focus_density
        )
        model_evaluations.append(summary)

        # Reporte decorativo (se mantiene en el log, como el usuario prefiere)
        print_cluster_report(summary)

    # Guardar resultados JSON
    output_file = output_dir / "clustered_evidences.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(model_evaluations, f, indent=2, ensure_ascii=False)
    writeLog("info", logger, f"[ClusterEvidences] Guardado en {output_file}")

    # Resumen estadístico final (con métricas mejoradas)
    writeLog("info", logger, "\n" + "=" * 80)
    writeLog("info", logger, "📊 RESUMEN ESTADÍSTICO DE CLUSTERING")
    writeLog("info", logger, "=" * 80)

    total_clusters = 0
    total_evidences = 0
    paper_concept_assignments = 0   # Un paper en 3 conceptos cuenta 3 veces
    all_unique_papers = set()       # Conjunto único real

    for summary in model_evaluations:
        concept_id = summary["concept_id"]
        concept_query = summary["concept_query"]
        n_clusters = len(summary["clusters"])
        n_evidences = summary["total_evidences"]
        concept_papers = set()
        for cluster in summary["clusters"]:
            concept_papers.update(cluster["papers"])
        all_unique_papers.update(concept_papers)

        total_clusters += n_clusters
        total_evidences += n_evidences
        paper_concept_assignments += len(concept_papers)

        query_short = concept_query[:80] + "..." if len(concept_query) > 80 else concept_query

        writeLog("info", logger,
            f"Concepto {concept_id} [focus_density={summary.get('focus_density', 0):.2f}]: "
            f"{query_short} — {n_evidences} evidencias, {n_clusters} clusters, "
            f"{len(concept_papers)} papers"
        )

    writeLog("info", logger, "-" * 80)
    writeLog("info", logger, f"Total evidencias procesadas: {total_evidences}")
    writeLog("info", logger, f"Total clusters formados: {total_clusters}")
    writeLog("info", logger,
        f"Asignaciones paper-concepto: {paper_concept_assignments} "
        f"(un mismo paper puede contar en varios conceptos)")
    writeLog("info", logger, f"Papers únicos en el proyecto: {len(all_unique_papers)}")
    writeLog("info", logger, "=" * 80)


if __name__ == "__main__":
    processClusterEvidences()