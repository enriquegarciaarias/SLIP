# 📘 Configuración — SLIP Pipeline

Este documento detalla todas las secciones de `config.json` y cómo afectan al comportamiento del pipeline.

## Estructura general

`config.json` se divide en:

| Sección | Descripción |
|---|---|
| `uid` | Identificador único del estudio |
| `defaults` | Parámetros globales del pipeline (modelos, pesos, secciones, umbrales) |
| `environment` | Rutas de directorios (input, output, cache, etc.) |
| `datasetVars` | Variables del dataset (extensible) |
| `params` | Parámetros adicionales (extensible) |

> El repositorio no incluye `config.json` real (contiene claves privadas). Créalo a partir del ejemplo:
> ```bash
> cp config_example.json config.json
> ```

---

## defaults.section_weights

Define los pesos por sección para el **section-weighted embedding** (discovery y evidence scoring). Se divide en `presets` y `section_aliases`.

### Presets disponibles

| Preset | Descripción | Uso típico |
|---|---|---|
| `technical_paper` | Favorece title, methodology, results, conclusion | Descubrimiento de topics técnicos |
| `evidence_scoring` | Favorece results, discussion, conclusion; atenúa introduction/background | Extracción de evidencias |

### Ejemplo `technical_paper`

```json
"technical_paper": {
    "title": 2.0,
    "abstract": 1.5,
    "methodology": 2.0,
    "methods": 2.0,
    "results": 2.0,
    "findings": 2.0,
    "discussion": 1.0,
    "conclusion": 1.2,
    "limitations": 0.7,
    "introduction": 0.5,
    "background": 0.3,
    "related_work": 0.2,
    "figures_tables_captions": 1.8,
    "acknowledgments": 0.0,
    "funding": 0.0,
    "references": 0.0,
    "bibliography": 0.0
}
```

### Sección `figures_tables_captions`

Peso **1.8** en ambos presets. Se llena automáticamente con los captions de tablas y figuras extraídos de los PDFs (solo el texto descriptivo del caption, no el contenido celular de las tablas, que es ruidoso).

### Aliases de sección

`section_aliases` normaliza nombres de sección variants:

| Alias | Canónico |
|---|---|
| `methods` | `methodology` |
| `findings` | `results` |
| `background` | `related_work` |
| `related work` | `related_work` |
| `related-work` | `related_work` |

---

## defaults.discovery

Configuración del motor de descubrimiento de topics (query-driven SLR orientado a RQ).

| Clave | Valor por defecto | Descripción |
|---|---|---|
| `embedding_model` | `BAAI/bge-base-en-v1.5` | Modelo de embeddings para secciones y queries |
| `section_filter_threshold` | 0.3 | Umbral de similitud para filtrar secciones relevantes |
| `section_filter_alpha` | 0.5 | Mezcla entre similitud full y focus |
| `off_topic_threshold` | 0.6 | Umbral base para filtrar conceptos off-topic |
| `off_topic_alpha` | 0.5 | Ajuste dinámico del umbral off-topic |
| `min_topics_per_rq` | 2 | Mínimo de topics por RQ |
| `min_papers_per_topic` | 2 | Mínimo de papers por topic |
| `n_seed_terms` | 30 | Número de términos semilla |
| `synthesis_min_tokens` | 300 | Mínimo de tokens para síntesis LLM |
| `synthesis_max_tokens` | 600 | Máximo de tokens para síntesis LLM |
| `synthesis_max_chars` | 4000 | Máximo de caracteres para síntesis LLM |
| `section_weights_preset` | `technical_paper` | Preset de pesos de sección |
| `section_weight_overrides` | `{"abstract": 1.8}` | Sobrescribes específicos |

> `section_weight_overrides` permite modificar un peso del preset sin redefinir todo el preset.

---

## defaults.conceptEvidence

Configuración del módulo de extracción de evidencias.

| Clave | Valor por defecto | Descripción |
|---|---|---|
| `embedding_model` | `BAAI/bge-base-en-v1.5` | Modelo de embeddings para evidencias |
| `section_weights_preset` | `evidence_scoring` | Preset de pesos para evidencias |
| `section_weight_overrides` | `{}` | Sobrescribes de pesos |
| `default_section_weight` | 0.7 | Peso para secciones no mapeadas |
| `genericity_penalty` | 0.5 | Penalización por genericidad cross-paper (solo orden) |
| `genericity_topk` | 5 | Top-k vecinos cross-paper para genericidad |
| `generic_threshold` | 0.8 | Umbral para considerar evidencia genérica (→ consenso) |
| `consensus_enabled` | true | Activar separación consenso/diferencial |
| `consensus_cluster_threshold` | 0.2 | Distancia coseno para agrupar genéricos |
| `zone_weight_enabled` | true | Activar scoring híbrido por zona argumentativa |
| `zone_weights.claim` | 2.0 | Peso para párrafos CLAIM |
| `zone_weights.evidence` | 1.8 | Peso para párrafos EVIDENCE |
| `zone_weights.method` | 1.2 | Peso para párrafos METHOD |
| `zone_weights.gap` | 0.8 | Peso para párrafos GAP |
| `zone_weights.background` | 0.3 | Peso para párrafos BACKGROUND |
| `zone_weights.rest` | 0.0 | Peso para párrafos REST |
| `default_zone_weight` | 1.0 | Peso para zonas sin mapear |

> Los pesos de zona se multiplican por los pesos de sección para obtener el peso combinado de cada ventana de evidencia.

---

## defaults.conceptMining

Configuración del módulo de minería de conceptos (keywords).

| Clave | Valor por defecto | Descripción |
|---|---|---|
| `top_k_keywords` | 30 | Número de keywords por concepto |
| `ngram_range` | [1, 3] | Rango de n-grams para KeyBERT |
| `semantic_dedup_enabled` | true | Activar deduplicación semántica de keywords |
| `semantic_dedup_threshold` | 0.85 | Umbral de similitud coseno para dedup |

> La deduplicación semántica usa embeddings de `BAAI/bge-base-en-v1.5` para eliminar keywords muy similares dentro de un mismo tópico, manteniendo el de mayor prioridad.

---

## defaults.structureExtraction

Configuración del módulo de extracción estructurada con LLM.

| Clave | Valor por defecto | Descripción |
|---|---|---|
| `llm_cache_enabled` | true | Activar cache persistente de respuestas del LLM |

> El cache se guarda en `results/output/{subject}/llm_cache.json`. Si el mismo texto + paper_id ya fue procesado, se reutiliza la respuesta sin llamar al LLM (ahorra tiempo y cuota de modelo).

---

## defaults.argumentativeZoning

Configuración del módulo de zonificación argumentativa.

| Clave | Valor por defecto | Descripción |
|---|---|---|
| `llm_refiner_enabled` | false | Activar refiner LLM para párrafos de baja confianza |
| `llm_refiner_confidence_threshold` | 0.6 | Umbral de confianza heurística para disparar refiner LLM |

> Cuando está habilitado, los párrafos clasificados con confianza < 0.6 por el clasificador heurístico se envían al LLM para reclasificación. Se usa solo si `llm_refiner_enabled: true` (por rendimiento).

---

## defaults.fineTuning

Configuración del módulo de fine-tuning de embeddings por investigación.

| Clave | Valor por defecto | Descripción |
|---|---|---|
| `base_model` | `BAAI/bge-base-en-v1.5` | Modelo base para fine-tuning |
| `output_model_name` | `slip-finetuned` | Nombre del directorio de salida del modelo |
| `epochs` | 1 | Número de épocas de entrenamiento |
| `batch_size` | 4 | Tamaño de batch (ajustar a GPU; 16 si hay VRAM suficiente) |
| `learning_rate` | 2e-5 | Tasa de aprendizaje |
| `max_pairs` | 2000 | Máximo de pares de entrenamiento generados |
| `pos_neg_ratio` | 1.0 | Relación positivos:negativos |
| `min_text_len` | 30 | Longitud mínima de texto para considerar |
| `max_text_len` | 2000 | Longitud máxima de texto (truncado) |
| `seed` | 42 | Semilla aleatoria |

> El modelo fine-tuneado se guarda en `results/input/{subject}/models/slip-{subject}/` y se usa automáticamente en ejecuciones posteriores de esa misma investigación.

---

## defaults.section_weights_preset (en discovery)

El preset activo para discovery se elige con `section_weights_preset` dentro de `defaults.discovery`. El valor por defecto es `"technical_paper"`.

---

## Configuración por investigación

SLIP es multi-investigación. Cada subject tiene su propio `config.json` en:

```
results/input/{subject}/config.json
```

Este config **sobrescribe** (merge profundo) las claves del `config.json` global. Ejemplo para `SensoresACL`:

```json
{
    "fineTuning": {
        "base_model": "BAAI/bge-base-en-v1.5",
        "output_model_name": "slip-sensores",
        "epochs": 1,
        "batch_size": 16,
        "learning_rate": 2e-5,
        "max_pairs": 2000,
        "pos_neg_ratio": 1.0
    },
    "discovery": {
        "embedding_model": "./models/slip-sensores",
        "embedding_model_dim": 768,
        "section_weights_preset": "technical_paper",
        "section_weight_overrides": {
            "abstract": 1.8
        }
    }
}
```

> Las rutas relativas en `embedding_model` (ej. `./models/slip-sensores`) se resuelven respecto a `results/input/{subject}/`.

---

## Cambios en v2.0

- `figure_tables_captions` como sección virtual con peso 1.8 (tanto en `technical_paper` como en `evidence_scoring`).
- `zone_weights` en `conceptEvidence` para scoring híbrido por zona argumentativa (CLAIM > EVIDENCE > METHOD > GAP > BACKGROUND > REST).
- `semantic_dedup_threshold` y `semantic_dedup_enabled` en `conceptMining`.
- `max_emerging_topics` en `training_materials` (límite de tópicos emergentes generados por LLM).
- `llm_refiner_enabled` / `llm_refiner_confidence_threshold` en `argumentativeZoning`.
- `llm_cache_enabled` en `structureExtraction` (cache persistente cross-run).
- Fine-tuning de embeddings por investigación en `results/input/{subject}/models/`.
