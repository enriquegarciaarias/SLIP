"""
fineTuningEngine.py
===================

Fine-tuning de modelos de embeddings por investigación (SLR).

SLIP es un pipeline multi-investigación: cada investigación (ej. sensores,
educación, ciberseguridad) tiene su propio corpus, RQs y dominio semántico.
Este módulo permite fine-tunear un modelo de embeddings base (por defecto
BAAI/bge-base-en-v1.5) usando los datos que SLIP ya genera, de modo que el
modelo entienda mejor la jerga y relaciones de esa investigación concreta.

Estrategia de pares de entrenamiento (automática, sin etiquetado manual):

  PAR POSITIVO (similares):
    - RQ <-> párrafo CLAIM/EVIDENCE de un paper asignado a esa RQ
    - Párrafo CLAIM <-> párrafo EVIDENCE del mismo paper (zona argumentativa)
    - Paper <-> su concepto asignado (doc_topic_map)

  PAR NEGATIVO (diferentes):
    - RQ <-> párrafo BACKGROUND/REST de un paper NO asignado a esa RQ
    - Párrafo CLAIM <-> párrafo BACKGROUND de papers distintos

Estructura de directorios:

  results/input/<investigacion>/
      studyDescription.json
      conceptsQuery.json
      config.json                 <- config específico de la investigación
      models/
          slip-<investigacion>/   <- fine-tuning específico guardado aquí

  results/models/base/
      slip-scientific-base/       <- fine-tuning general (punto de partida)

El modelo resultante se referencia en config.json de la investigación:

  "discovery": {
      "embedding_model": "./models/slip-sensores",
      "embedding_model_dim": 768
  }

Uso:

  python sources/fineTuningEngine.py --subject SensoresACL

O desde el pipeline:

  from sources.fineTuningEngine import processFineTuning
  processFineTuning(subject="SensoresACL")
"""

from __future__ import annotations

import json
import os
import random
import hashlib

# Evitar fragmentación de memoria CUDA en GPUs con poca VRAM
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo


# ============================================================
# CONFIGURACIÓN DE FINE-TUNING
# ============================================================

@dataclass
class FineTuningConfig:
    """Parámetros de fine-tuning. Sobrescribibles desde config.json."""
    base_model: str = "BAAI/bge-base-en-v1.5"
    output_model_name: str = "slip-finetuned"
    incremental: bool = False      # Si True y existe modelo previo, usarlo como base
    epochs: int = 1
    batch_size: int = 4
    learning_rate: float = 2e-5
    max_pairs: int = 2000          # máximo de pares de entrenamiento a generar
    pos_neg_ratio: float = 1.0     # relación positivos:negativos
    min_text_len: int = 30         # longitud mínima de texto para considerar
    max_text_len: int = 2000       # longitud máxima truncada
    seed: int = 42


# ============================================================
# CARGA DE DATOS DE ENTRENAMIENTO
# ============================================================

def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        writeLog("warning", logger, f"[FineTuning] Error cargando {path}: {exc}")
        return None


def _text_ok(text: Optional[str], min_len: int, max_len: int) -> bool:
    if not text or not isinstance(text, str):
        return False
    t = text.strip()
    return min_len <= len(t) <= max_len


def build_training_pairs(
    input_dir: Path,
    output_dir: Path,
    cfg: FineTuningConfig,
) -> List[Tuple[str, str, float]]:
    """
    Construye pares (texto_a, texto_b, similitud) para fine-tuning contrastive.

    similitud = 1.0 (positivo) o 0.0 (negativo).

    Fuentes usadas:
      - papers_structured.json : contribuciones, metodología, resultados
      - papers_zoned.json      : párrafos CLAIM/EVIDENCE/METHOD/BACKGROUND
      - aligned_concepts.json  : conceptos asignados a papers (doc_topic_map)
      - conceptsQuery.json     : RQs como anclas
    """
    random.seed(cfg.seed)
    np.random.seed(cfg.seed)

    pairs: List[Tuple[str, str, float]] = []
    seen_pairs: set = set()

    # ----------------------------------------------------------
    # 1. Cargar fuentes
    # ----------------------------------------------------------
    papers_structured: List[Dict] = _load_json(output_dir / "papers_structured.json") or []
    papers_zoned: List[Dict] = _load_json(output_dir / "papers_zoned.json") or []
    aligned_concepts: Dict = _load_json(output_dir / "aligned_concepts.json") or {}
    concepts_query: Dict = _load_json(input_dir / "conceptsQuery.json") or {}

    writeLog("info", logger,
             f"[FineTuning] Fuentes: {len(papers_structured)} structured, "
             f"{len(papers_zoned)} zoned, {len(aligned_concepts.get('aligned_concepts', []))} aligned concepts")

    # Indexar zoned por paper_id para acceso rápido
    zoned_by_paper: Dict[str, List[Dict]] = {}
    for p in papers_zoned:
        pid = p.get("paper_id")
        if pid:
            zoned_by_paper[pid] = p.get("paragraphs", [])

    # Indexar structured por paper_id
    struct_by_paper: Dict[str, Dict] = {}
    for p in papers_structured:
        pid = p.get("paper_id")
        if pid:
            struct_by_paper[pid] = p

    # ----------------------------------------------------------
    # 2. Pares positivos desde zoning (CLAIM <-> EVIDENCE mismo paper)
    # ----------------------------------------------------------
    for pid, paragraphs in zoned_by_paper.items():
        claims = [pa["text"] for pa in paragraphs if pa.get("zone") == "claim"]
        evidences = [pa["text"] for pa in paragraphs if pa.get("zone") == "evidence"]
        methods = [pa["text"] for pa in paragraphs if pa.get("zone") == "method"]

        # CLAIM <-> EVIDENCE (mismo paper, relacionado semánticamente)
        for c in claims:
            if not _text_ok(c, cfg.min_text_len, cfg.max_text_len):
                continue
            for e in evidences:
                if not _text_ok(e, cfg.min_text_len, cfg.max_text_len):
                    continue
                key = (c[:80], e[:80], 1.0)
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    pairs.append((c, e, 1.0))

        # CLAIM <-> METHOD (mismo paper)
        for c in claims:
            if not _text_ok(c, cfg.min_text_len, cfg.max_text_len):
                continue
            for m in methods:
                if not _text_ok(m, cfg.min_text_len, cfg.max_text_len):
                    continue
                key = (c[:80], m[:80], 1.0)
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    pairs.append((c, m, 1.0))

    writeLog("info", logger, f"[FineTuning] Pares positivos por zoning: {len(pairs)}")

    # ----------------------------------------------------------
    # 3. Pares positivos desde aligned_concepts (concepto <-> paper)
    # ----------------------------------------------------------
    rq_texts: List[str] = []
    for concept in aligned_concepts.get("aligned_concepts", []):
        query = concept.get("concept_query") or concept.get("concept_name", "")
        if _text_ok(query, cfg.min_text_len, cfg.max_text_len):
            rq_texts.append(query)

        docs = concept.get("documents", [])
        for doc in docs:
            doc_id = doc.get("doc_id", "")
            paper_title = doc.get("title", "")
            # Buscar texto del paper en structured
            p_struct = struct_by_paper.get(doc_id, {})
            paper_text = ""
            if p_struct:
                # Usar contribuciones + methodology + results como representación
                parts = []
                contribs = p_struct.get("contributions", [])
                if contribs:
                    parts.extend(contribs[:3])
                method = p_struct.get("methodology", {})
                if isinstance(method, dict):
                    for k in ["approach", "architecture", "datasets", "metrics"]:
                        v = method.get(k)
                        if v:
                            if isinstance(v, list):
                                parts.extend(v)
                            else:
                                parts.append(str(v))
                results = p_struct.get("results", {})
                if isinstance(results, dict):
                    if results.get("main"):
                        parts.append(str(results["main"]))
                paper_text = " ".join(parts)

            if not paper_text and doc_id in zoned_by_paper:
                # Fallback: usar párrafos CLAIM/EVIDENCE del paper
                paras = zoned_by_paper[doc_id]
                relevant = [pa["text"] for pa in paras
                            if pa.get("zone") in ("claim", "evidence")]
                paper_text = " ".join(relevant[:3])

            if _text_ok(query, cfg.min_text_len, cfg.max_text_len) and _text_ok(paper_text, cfg.min_text_len, cfg.max_text_len):
                key = (query[:80], paper_text[:80], 1.0)
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    pairs.append((query, paper_text, 1.0))

    writeLog("info", logger, f"[FineTuning] Pares positivos por conceptos: {len(pairs)}")

    # ----------------------------------------------------------
    # 4. Pares positivos desde RQs (conceptQuery)
    # ----------------------------------------------------------
    for concept in concepts_query.get("concepts", []):
        query = concept.get("query", "") or concept.get("name", "")
        desc = concept.get("description", "")
        rq_text = f"{query} {desc}".strip()
        if not _text_ok(rq_text, cfg.min_text_len, cfg.max_text_len):
            continue

        # Los papers asociados a esta RQ (desde aligned_concepts) son positivos
        for concept_aligned in aligned_concepts.get("aligned_concepts", []):
            if concept_aligned.get("concept_query") == query or concept_aligned.get("concept_name") == concept.get("name"):
                for doc in concept_aligned.get("documents", []):
                    doc_id = doc.get("doc_id", "")
                    p_struct = struct_by_paper.get(doc_id, {})
                    paper_text = ""
                    if p_struct:
                        contribs = p_struct.get("contributions", [])
                        if contribs:
                            paper_text = " ".join(contribs[:3])
                    if not paper_text and doc_id in zoned_by_paper:
                        paras = zoned_by_paper[doc_id]
                        relevant = [pa["text"] for pa in paras if pa.get("zone") in ("claim", "evidence")]
                        paper_text = " ".join(relevant[:3])
                    if _text_ok(paper_text, cfg.min_text_len, cfg.max_text_len):
                        key = (rq_text[:80], paper_text[:80], 1.0)
                        if key not in seen_pairs:
                            seen_pairs.add(key)
                            pairs.append((rq_text, paper_text, 1.0))

    writeLog("info", logger, f"[FineTuning] Pares positivos por RQs: {len(pairs)}")

    # ----------------------------------------------------------
    # 5. Pares negativos (muestreo aleatorio de textos no relacionados)
    # ----------------------------------------------------------
    all_texts: List[str] = []
    for pid, paragraphs in zoned_by_paper.items():
        for pa in paragraphs:
            if _text_ok(pa.get("text", ""), cfg.min_text_len, cfg.max_text_len):
                all_texts.append(pa["text"])
    for pid, p_struct in struct_by_paper.items():
        contribs = p_struct.get("contributions", [])
        for c in contribs:
            if _text_ok(c, cfg.min_text_len, cfg.max_text_len):
                all_texts.append(c)

    # Deduplicar textos
    all_texts = list(dict.fromkeys(all_texts))

    # Anclas para negativos: usar RQs y CLAIMs
    anchors: List[str] = list(dict.fromkeys(rq_texts))
    for pid, paragraphs in zoned_by_paper.items():
        for pa in paragraphs:
            if pa.get("zone") == "claim" and _text_ok(pa.get("text", ""), cfg.min_text_len, cfg.max_text_len):
                anchors.append(pa["text"])
    anchors = list(dict.fromkeys(anchors))

    n_pos = len([p for p in pairs if p[2] == 1.0])
    n_neg_target = int(n_pos * cfg.pos_neg_ratio)
    n_neg_target = min(n_neg_target, len(anchors) * len(all_texts) // 10)  # no explotar

    neg_added = 0
    attempts = 0
    max_attempts = n_neg_target * 20 if n_neg_target > 0 else 0

    while neg_added < n_neg_target and attempts < max_attempts:
        attempts += 1
        a = random.choice(anchors)
        b = random.choice(all_texts)
        if a == b:
            continue
        key = (a[:80], b[:80], 0.0)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        pairs.append((a, b, 0.0))
        neg_added += 1

    writeLog("info", logger,
             f"[FineTuning] Pares negativos generados: {neg_added}")

    # ----------------------------------------------------------
    # 6. Mezclar y limitar
    # ----------------------------------------------------------
    random.shuffle(pairs)
    if len(pairs) > cfg.max_pairs:
        pairs = pairs[:cfg.max_pairs]

    writeLog("info", logger,
             f"[FineTuning] Total pares finales: {len(pairs)} "
             f"(positivos: {sum(1 for p in pairs if p[2] == 1.0)}, "
             f"negativos: {sum(1 for p in pairs if p[2] == 0.0)})")

    return pairs


# ============================================================
# FINE-TUNING CON SENTENCE-TRANSFORMERS
# ============================================================

def run_fine_tuning(
    pairs: List[Tuple[str, str, float]],
    cfg: FineTuningConfig,
    output_dir: Path,
) -> Optional[Path]:
    """
    Ejecuta el fine-tuning del modelo de embeddings usando sentence-transformers.

    Devuelve la ruta al directorio del modelo fine-tuneado, o None si falla.
    """
    try:
        from sentence_transformers import SentenceTransformer, InputExample, losses
        from torch.utils.data import DataLoader
    except ImportError as exc:
        writeLog("error", logger,
                 f"[FineTuning] sentence-transformers/torch no disponible: {exc}")
        return None

    if not pairs:
        writeLog("warning", logger, "[FineTuning] No hay pares de entrenamiento. Abortando.")
        return None

    writeLog("info", logger,
             f"[FineTuning] Cargando modelo base: {cfg.base_model}")
    model = SentenceTransformer(cfg.base_model)

    # Construir InputExamples
    train_examples = []
    for text_a, text_b, score in pairs:
        train_examples.append(InputExample(texts=[text_a[:cfg.max_text_len], text_b[:cfg.max_text_len]], label=float(score)))

    train_dataloader = DataLoader(train_examples, shuffle=True, batch_size=cfg.batch_size)
    train_loss = losses.CosineSimilarityLoss(model)

    # Directorio de salida
    model_output = output_dir / "models" / cfg.output_model_name
    model_output.mkdir(parents=True, exist_ok=True)

    writeLog("info", logger,
             f"[FineTuning] Entrenando: {len(train_examples)} ejemplos, "
             f"{cfg.epochs} épocas, batch={cfg.batch_size}, lr={cfg.learning_rate}")

    try:
        model.fit(
            train_objectives=[(train_dataloader, train_loss)],
            epochs=cfg.epochs,
            warmup_steps=int(len(train_dataloader) * cfg.epochs * 0.1),
            output_path=str(model_output),
            show_progress_bar=True,
        )
    except TypeError:
        # Algunas versiones de sentence-transformers no aceptan output_path en fit
        model.fit(
            train_objectives=[(train_dataloader, train_loss)],
            epochs=cfg.epochs,
            warmup_steps=int(len(train_dataloader) * cfg.epochs * 0.1),
            show_progress_bar=True,
        )
        model.save(str(model_output))

    writeLog("info", logger, f"[FineTuning] Modelo guardado en: {model_output}")
    return model_output


# ============================================================
# PUNTO DE ENTRADA
# ============================================================

def load_fine_tuning_config(subject: str, input_dir: Optional[Path] = None) -> FineTuningConfig:
    """Carga configuración de fine-tuning desde config.json de la investigación."""
    cfg = FineTuningConfig()
    # Buscar config.json de la investigación
    if input_dir is None:
        results_root = processControl.env.get("results_root") or "results"
        input_dir = Path(processControl.env.get("realPath", "")) / results_root / "input" / subject
    config_file = input_dir / "config.json"
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            ft_cfg = data.get("fineTuning", {})
            if isinstance(ft_cfg, dict):
                cfg.base_model = ft_cfg.get("base_model", cfg.base_model)
                cfg.output_model_name = ft_cfg.get("output_model_name", cfg.output_model_name)
                cfg.incremental = ft_cfg.get("incremental", cfg.incremental)
                cfg.epochs = int(ft_cfg.get("epochs", cfg.epochs))
                cfg.batch_size = int(ft_cfg.get("batch_size", cfg.batch_size))
                cfg.learning_rate = float(ft_cfg.get("learning_rate", cfg.learning_rate))
                cfg.max_pairs = int(ft_cfg.get("max_pairs", cfg.max_pairs))
                cfg.pos_neg_ratio = float(ft_cfg.get("pos_neg_ratio", cfg.pos_neg_ratio))
                cfg.min_text_len = int(ft_cfg.get("min_text_len", cfg.min_text_len))
                cfg.max_text_len = int(ft_cfg.get("max_text_len", cfg.max_text_len))
                cfg.seed = int(ft_cfg.get("seed", cfg.seed))
        except Exception as exc:
            writeLog("warning", logger,
                     f"[FineTuning] Error cargando config.json de {subject}: {exc}. Usando defaults.")
    return cfg


def _compute_corpus_checksum(input_dir: Path, output_dir: Path) -> str:
    """
    Calcula un checksum basado en los archivos que definen el corpus de la investigación.
    Si estos archivos cambian, el fine-tuning debe regenerarse.
    """
    files_to_hash = [
        input_dir / "conceptsQuery.json",
        output_dir / "papers_structured.json",
        output_dir / "aligned_concepts.json",
        output_dir / "papers_zoned.json",
    ]
    h = hashlib.sha256()
    for f in files_to_hash:
        if f.exists():
            try:
                with open(f, "rb") as fh:
                    h.update(fh.read())
            except Exception:
                pass
    return h.hexdigest()


def _load_saved_checksum(model_path: Path) -> Optional[str]:
    """Carga el checksum guardado junto al modelo fine-tuneado."""
    checksum_file = model_path / "corpus_checksum.txt"
    if checksum_file.exists():
        try:
            return checksum_file.read_text(encoding="utf-8").strip()
        except Exception:
            return None
    return None


def _save_checksum(model_path: Path, checksum: str) -> None:
    """Guarda el checksum junto al modelo fine-tuneado."""
    checksum_file = model_path / "corpus_checksum.txt"
    try:
        checksum_file.write_text(checksum, encoding="utf-8")
    except Exception as exc:
        writeLog("warning", logger,
                 f"[FineTuning] No se pudo guardar checksum: {exc}")


def processFineTuning(subject: Optional[str] = None) -> Dict[str, Any]:
    """
    Punto de entrada del módulo de fine-tuning con auto-detección.

    El operador siempre ejecuta el mismo comando. Este módulo decide automáticamente:
      - Si NO existe modelo fine-tuneado → entrena y guarda el modelo.
      - Si YA existe modelo y el corpus NO cambió → salta el entrenamiento (usa el existente).
      - Si YA existe modelo pero el corpus SÍ cambió → regenera el fine-tuning.

    Args:
        subject: Nombre de la investigación (ej. "SensoresACL"). Si None, se lee
                 de processControl.args.subject.

    Returns:
        Dict con claves:
          - "fine_tuning_generated": bool (True si se entrenó un modelo nuevo en esta ejecución)
          - "model_path": Optional[Path] (ruta al modelo fine-tuneado, existente o nuevo)
    """
    result: Dict[str, Any] = {
        "fine_tuning_generated": False,
        "model_path": None,
    }

    if subject is None:
        args = getattr(processControl, "args", None)
        subject = args.subject if args and hasattr(args, "subject") else "SensoresACL"

    input_dir, output_dir = inicioModulo("processFineTuning")

    # Asegurar que el directorio de modelos exista en input/<subject>/models/
    models_dir = input_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    cfg = load_fine_tuning_config(subject, input_dir=input_dir)

    model_path = models_dir / cfg.output_model_name

    # ----------------------------------------------------------
    # Auto-detección: ¿ya existe modelo válido?
    # ----------------------------------------------------------
    current_checksum = _compute_corpus_checksum(input_dir, output_dir)
    saved_checksum = _load_saved_checksum(model_path)

    if model_path.exists() and saved_checksum == current_checksum:
        writeLog("info", logger,
                 f"[FineTuning] ✅ Modelo fine-tuneado ya existe y el corpus no cambió. Saltando entrenamiento.")
        writeLog("info", logger,
                 f"[FineTuning] Modelo: {model_path}")
        writeLog("info", logger,
                 f"[FineTuning] discoveryEngine usará automáticamente este modelo.")
        result["fine_tuning_generated"] = False
        result["model_path"] = model_path
        return result

    if model_path.exists() and saved_checksum != current_checksum:
        writeLog("info", logger,
                 f"[FineTuning] ⚠️  El corpus cambió desde el último fine-tuning. Regenerando modelo...")
    else:
        writeLog("info", logger,
                 f"[FineTuning] No existe modelo fine-tuneado para {subject}. Entrenando primero...")

    # Fine-tuning incremental: si está habilitado y existe modelo previo,
    # usarlo como base en lugar del modelo base original.
    if cfg.incremental and model_path.exists():
        cfg.base_model = str(model_path)
        writeLog("info", logger,
                 f"[FineTuning] 🔄 Fine-tuning incremental: usando modelo previo como base: {model_path}")
    else:
        writeLog("info", logger,
                 f"[FineTuning] Base model: {cfg.base_model} | Epochs: {cfg.epochs} | "
                 f"Batch: {cfg.batch_size} | Max pairs: {cfg.max_pairs}")

    # Construir pares de entrenamiento (necesita input_dir para conceptsQuery.json)
    pairs = build_training_pairs(input_dir, output_dir, cfg)

    if not pairs:
        writeLog("error", logger,
                 "[FineTuning] No se generaron pares de entrenamiento. Abortando.")
        result["fine_tuning_generated"] = False
        result["model_path"] = None
        return result

    # Ejecutar fine-tuning (guarda en input_dir/models/<output_model_name>)
    model_path = run_fine_tuning(pairs, cfg, input_dir)

    if model_path:
        _save_checksum(model_path, current_checksum)
        result["fine_tuning_generated"] = True
        result["model_path"] = model_path
        writeLog("info", logger,
                 f"[FineTuning] ✅ Completo. Modelo en: {model_path}")
        writeLog("info", logger,
                 f"[FineTuning] Checksum guardado para detección de cambios futuros.")
        writeLog("info", logger,
                 f"[FineTuning] 📢 El modelo fine-tuneado estará disponible para discoveryEngine "
                 f"en una pasada posterior del pipeline.")
    else:
        writeLog("error", logger, "[FineTuning] ❌ Falló el fine-tuning.")
        result["fine_tuning_generated"] = False
        result["model_path"] = None

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", type=str, default="SensoresACL")
    args = parser.parse_args()

    from sources.common.paramsManager import getConfigs
    getConfigs()
    processFineTuning(subject=args.subject)
