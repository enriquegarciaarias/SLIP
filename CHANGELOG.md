# 📜 CHANGELOG — SLIP Pipeline

Todas las mudanças notables del proyecto SLIP se documentan en este archivo.

El formato se basa en [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [v2.0] — 2026-10-16

### 🚀 Nuevas funcionalidades

#### Argumentative Zoning con refiner LLM
- Nuevo módulo `sources/argumentativeZoning.py` que clasifica párrafos en zonas retóricas: CLAIM, EVIDENCE, METHOD, BACKGROUND, GAP, FUTURE_WORK, REST.
- Clasificador heurístico basado en patrones lingüísticos + sección.
- **LLM refiner opcional** (`llm_refiner_enabled`): para párrafos de baja confianza (< 0.6), se invoca al LLM para reclasificar.
- Pesos por zona: claim=2.0, evidence=1.8, method=1.2, gap=0.8, background=0.3, rest=0.0.

#### Extracción de Figure/Table Captions
- `corpusCleaning` y `fullTextExtractionEngine` extraen captions de tablas y figuras de los PDFs usando regex.
- Los captions se agregan como sección virtual `figures_tables_captions` en `papers_text.json`.
- Peso 1.8 en ambos presets (`technical_paper` y `evidence_scoring`).
- **Solo se extrae el caption (texto descriptivo), no el contenido celular de las tablas** (evita ruido de celdas vacías, siglas rotadas, números sueltos).

#### Fine-tuning de embeddings por investigación
- Nuevo módulo `sources/fineTuningEngine.py`.
- Genera pares de entrenamiento automáticamente (sin etiquetado manual) a partir de papers_zoned.json, aligned_concepts.json, papers_structured.json y conceptsQuery.json.
- Fine-tunea `BAAI/bge-base-en-v1.5` y guarda el modelo en `results/input/{subject}/models/slip-{subject}/`.
- **Auto-detección**: si existe modelo fine-tuneado y el corpus no cambió (checksum SHA-256), se salta el entrenamiento.
- **Auto-reinicio**: si se genera modelo nuevo en la primera pasada, el pipeline se reinicia para que discoveryEngine lo use.
- Configurable por investigación en `results/input/{subject}/config.json`.

#### Section-Weighted Embeddings (ya implementado, documentado en v2.0)
- Pesos por sección para embeddings de papers (title=2.0, methodology=2.0, results=2.0, etc.).
- Presets: `technical_paper` y `evidence_scoring`.
- Aliases de sección para normalizar nombres (methods→methodology, findings→results, etc.).

#### Concept Mining Engine mejorado
- Inyección directa de términos estructurados (key_numbers, metrics, datasets, signal_modalities, baselines, tables) en KeyBERT.
- Prioridad: inyección directa > candidatos (focus + structured) > free terms.
- Enriquecimiento de texto con secciones virtuales (CONTRIBUTIONS, METHODOLOGY_STRUCTURED, RESULTS_STRUCTURED, TAKEAWAY, ARGUMENTATIVE_ZONES).
- **Deduplicación semántica** de keywords (coseno ≥ 0.85 elimina términos duplicados semánticamente).
- 36.7% keywords estructurados (vs 1-2% antes).

#### ConceptEvidence con scoring híbrido por zona
- Ponderación combinada: peso de sección × peso de zona argumentativa.
- Campo `zone` añadido al dataclass `Evidence` y serializado en `concept_evidence.json`.

#### Configuración por investigación
- `results/input/{subject}/config.json` sobrescribe (merge profundo) las claves del `config.json` global.
- Rutas relativas en `embedding_model` (ej. `./models/slip-sensores`) se resuelven respecto al directorio de input del subject.

#### Config LLM Cache en structureExtraction
- Cache persistente de respuestas del LLM en `llm_cache.json` para evitar re-llamadas entre runs.
- Habilitado por defecto (`llm_cache_enabled: true`).

#### Límite de tópicos emergentes
- `max_emerging_topics` en `training_materials` (default 20, 0 = sin límite) para evitar generación excesiva de tópicos por LLM.

#### Chat Inteligente con RAG (sources/chatEngine.py)
- Motor de chat con Retrieval-Augmented Generation sobre los resultados del pipeline.
- Carga `papers_text.json`, `concept_evidence.json`, `concept_findings.json` y `papers_structured.json`.
- Genera chunks de texto con embeddings `BAAI/bge-base-en-v1.5` y los almacena en memoria.
- Para cada pregunta, recupera los chunks más relevantes por similitud coseno.
- Usa Ollama (qwen3:8b) para generar respuestas con citas de fuentes (paper_id, sección, similitud).
- Interfaz Streamlit: `streamlit run sources/chatEngine.py -- --ui`
- Uso como módulo: `ChatEngine(output_dir).indexar().preguntar("...")`

#### Múltiples estrategias de búsqueda
- `conceptQuery.json` soporta múltiples estrategias bajo `query_strategies` (ej: "01", "02", "03").
- Cada estrategia tiene su propia query genérica y queries específicas por fuente (scopus, wos, ieee, etc.).
- Los archivos de exportación llevan sufijo de estrategia (ej: `scopus_export_01.csv`, `scopus_search_02.json`).
- Cada paper queda etiquetado con su `query_strategies` para trazabilidad en el PRISMA.
- `normalizeSearchResults` lee archivos con sufijo de estrategia y etiqueta cada paper.
- `searchMergeEngine` propaga `query_strategies` en el merge de documentos.
- `trainingMaterials.py` muestra conteos por estrategia en el diagrama PRISMA.

### 📊 Resultados clave (corpus SensoresACL, 71 papers)

| Métrica | Before (v1.x) | After (v2.0) | Mejora |
|---|---|---|---|
| Topics descubiertos | 38 | 44 | +16% |
| Docs cubiertos | 50 | 54 | +8% |
| Keywords estructurados | ~1.8% | 36.7% | 20x |
| Keywords métricas | 0 | 345 | ∞ |
| Keywords datasets | 0 | 45 | ∞ |
| RQs técnicas (0,1) | 16 topics | 27 topics | +69% |

### 🧪 Tests

- 91 tests unitarios totales (30 nuevos + 61 preexistentes), todos pasando.
- Nueva clase `TestDeduplicateSemanticTerms` (6 tests).
- Nueva clase `TestBuildEnrichedText` (10 tests).
- `TestExtractStructuredTerms` (10 tests).
- `TestLoadStructuredPapers` (3 tests) y `TestLoadZonedPapers` (2 tests).

---

## [v1.x] — Versiones previas

- Pipeline base E2E para revisión sistemática de literatura (SLR).
- Módulos: normalización, búsqueda, ranking, enriquecimiento, selección, adquisición, corpus cleaning, discovery, concept mining, alignment, evidence, clustering, synthesis, technical annex, training materials, related work.
- Section-weighted embeddings inicial.
- Structure extraction con LLM.
- Argumentative zoning heurístico (sin refiner LLM).

---

## ⚠️ Notas de migración v1.x → v2.0

- `papers_text.json` ahora incluye la sección `figures_tables_captions`.
- `concept_evidence.json` ahora incluye el campo `zone` en cada evidencia.
- `config.json` incluye nuevas secciones: `fineTuning`, `argumentativeZoning`, `structureExtraction.llm_cache_enabled`, `conceptMining.semantic_dedup_*`, `training_materials.max_emerging_topics`, `argumentativeZoning.llm_refiner_*`.
- `discovery.embedding_model` puede ser una ruta relativa a un modelo fine-tuneado en `results/input/{subject}/models/`.
- Los archivos de exportación pueden llevar sufijo de estrategia (ej: `scopus_export_01.csv`) cuando se usan múltiples estrategias de búsqueda.
- `sources/chatEngine.py` es un nuevo módulo para chat con RAG sobre los resultados del pipeline.
- Si se usa `llm_refiner_enabled: true` en `argumentativeZoning`, el tiempo de ejecución aumenta (llamadas LLM para párrafos de baja confianza).
- Si se usa `llm_cache_enabled: true` en `structureExtraction`, las ejecuciones posteriores son más rápidas (cache de respuestas LLM).
