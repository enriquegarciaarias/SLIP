# AGENTS.md — Contexto del proyecto SLIP

Este archivo resume el pipeline **SLIP (Scientific Literature Intelligence Pipeline)** para que cualquier agente pueda comenzar a trabajar sin reaprender la estructura. Úsalo como contexto inicial en cada nueva sesión.

---

## 1. Qué es SLIP

Pipeline secuencial de revisión sistemática de literatura (SLR). Parte de resultados de búsqueda bibliográfica (WoS, Scopus, IEEE, PubMed, OpenAlex), construye un corpus limpio de PDFs, extrae evidencia estructurada por preguntas de investigación (RQ) y genera documentos de síntesis:

- `training_materials.md` — PRISMA + hallazgos + tópicos emergentes
- `related_work.md` — Related Work listo para paper
- `technical_annex.json` — perfiles técnicos por paper

**Punto de entrada:** `main.py` → función `mainProcess()`.

---

## 2. Estructura de directorios

```
results/input/{subject}/                  # Entrada por asunto de investigación
├── studyDescription.json                 # Objetivos, keywords, focus_terms, campos técnicos
├── conceptsQuery.json                    # Query de búsqueda + conceptos/RQs (query_strategies opcional)
├── personalizacion.json                  # (opcional) Blacklist del investigador
├── config.json                           # Config específica del subject (merge profundo sobre la global)
├── wos_export.txt / scopus_export.csv / ieee_export.csv / pubmed_export.txt
├── scholar_export_01.csv / scholar_export_02.csv  # (opcional) exports de Google Scholar por estrategia
├── manual_papers/                        # PDFs semilla + manual_papers.json
└── manual_papers/manuscript/             # (opcional, --manuscript 1)
    ├── manuscript.pdf                    # nunca se ingesta como paper del corpus
    ├── main.tex                          # para extraer contextos de cita
    ├── biblio.bib                        # mapeo citas ↔ PDFs
    └── <bibkey>.pdf                      # PDFs asociados nombrados por bibkey

results/output/{subject}/                 # Salidas intermedias y finales
├── *_search.json                         # (wos, scopus, ieee, pubmed, openalex)_search.json
├── canonical.json                        # papers unificados tras merge (global_id)
├── ranked_papers.json
├── selected_papers.json
├── review_original.pdf, review_es.pdf
├── selection_checkpoint.json             # progreso de selección interactiva (reanudable)
├── selected_acquired.json
├── pdf_download_log.json                 # índice determinista paper_id → pdf_file + eventos
├── missing_pdfs.json
├── pdf_inventory.json                    # índice de la bóveda _pdf_vault
├── pdfs/                                 # PDFs descargados
├── _pdf_vault/                           # bóveda de PDFs para reutilizar entre ejecuciones
├── papers_metadata.json
├── papers_text.json                      # texto limpio + secciones + figures_tables_captions
├── papers_zoned.json                     # párrafos con zona argumentativa
├── papers_structured.json                # info estructurada por paper (LLM)
├── llm_cache.json                        # caché persistente de respuestas LLM
├── candidate_concepts.json
├── candidate_concepts_by_rq.json
├── concept_candidates.json
├── aligned_concepts.json
├── concept_evidence.json                 # knowledge units diferenciales + bloque consenso
├── clustered_evidences.json
├── enriched_evidences.json
├── concept_findings.json
├── narrative_summaries.json              # caché de resúmenes narrativos
├── technical_annex.json
├── training_materials.md
├── related_work.md
├── prisma.md                             # informe PRISMA dedicado (generado por completeSelection)
├── manuscript_refs.json                  # si --manuscript 1
├── manuscript_context_cache.json         # si --manuscript 1
├── translation_cache.json
└── notebook/                             # exportación para Gemini Notebook
    ├── gemini_notebook_01.md … _50.md    # máximo 50 papers/archivo, 200 MB/archivo
    └── gemini_notebook_export_summary.json

sources/                                  # todos los módulos del pipeline
config.json (raíz, gitignorado)           # config global; crear a partir de config_example.json
config_example.json
requirements.txt
streamlit_app.py                          # UI del chat RAG
main.py                                   # orquestador
SESSION_STATE.md                          # bitácora operativa de la última sesión
AGENTS.md                                 # este archivo
```

---

## 3. Configuración (`config.json`)

El repositorio **no incluye** `config.json` real (contiene claves privadas). Crearlo con:

```bash
cp config_example.json config.json
```

Secciones clave de `defaults`:

| Sección | Qué controla |
|---|---|
| `search.availables` / `search.providers.<fuente>` | fuentes activas (`wos`, `scopus`, `ieee`, `pubmed`, `openalex`, `arxiv`, `scholar`, `acl_anthology`) y modo `api`/`file` + credenciales. IEEE configurado en modo `"api"` con API key propia. ACL Anthology usa la librería `acl_anthology` (repositorio git local) y se configura en `search.providers.acl_anthology` con `mode: "api"`, `min_year` y `max_results`. |
| `search.decomposition.enabled` | además de la query global, lanza una consulta por pregunta de investigación |
| `search.merge.by_title_only_min_chars` | dedup por título sin año |
| `llm.primary_model` / `llm.fallback_model` | modelos Ollama (primary: qwen3:14b; fallbacks: qwen2.5:14b, qwen3:8b, qwen2.5:7b, llama3.1:8b) |
| `conceptEvidence.*` | `section_weights`, `genericity_penalty`, `genericity_topk`, `generic_threshold`, `consensus_enabled`, `consensus_cluster_threshold`, `zone_weights.*` |
| `url_blacklist` (en `config.json` global) | `urls` / `domains` (dominios de sitios inaccesibles, ej. `iopscience.iop.org`). Aplica a **todas** las investigaciones. Los papers con `landing_page`/`pdf_url` que coincidan se excluyen **antes del ranking** para no ocupar cupo de `top_k`. |
| `blacklist.*` (en `personalizacion.json` por subject) | `ids` (paper_ids a excluir), `urls` / `domains` (override adicional por subject). |
| `paper_blacklist` (en `config.json` por subject, `results/input/{subject}/config.json`) | `entries` (lista con `id`, `reason`, `detail`). Motivos: `low_text` (bajo contenido tras limpieza), `operator_rejected` (operador dijo "N"), `pdf_unreadable`). Los papers se excluyen **antes del ranking** y se reportan en `prisma.md` sección 6.6. Cada investigación tiene su propia lista de `paper_id`; las URLs/dominios (`url_blacklist`) se mantienen en el `config.json` global porque aplican a todas las investigaciones. |
| `discovery.*` | `embedding_model`, `section_weights_preset`, umbrales off-topic |
| `conceptMining.*` | `top_k_keywords`, `semantic_dedup_enabled`, `semantic_dedup_threshold` |
| `structureExtraction.llm_cache_enabled` | caché LLM persistente |
| `argumentativeZoning.*` | `llm_refiner_enabled`, `llm_refiner_confidence_threshold` |
| `fineTuning.*` | `base_model`, `incremental` (usar modelo previo como base si existe, fine-tuning incremental), `epochs`, `batch_size`, `max_pairs`, etc. |
| `training_materials.*` | `manuscript`, `emergente`, `max_emerging_topics` |
| `ranking.*` | `embedding_model`, `top_k`, `min_text_length`, `similarity_threshold` (umbral de similitud de coseno, aplicado ANTES del top_k; si no está definido, se usa 0.00 sin filtro) |
| `environment.results_root` | Ruta alternativa para el directorio `results/`. Si está definido y no vacío, se usa como base para `input` y `output` en lugar de `results/` bajo `realPath`. Si está vacío o no está definido, se aplica la configuración actual (`results/` en la raíz del proyecto). Puede ser absoluto o relativo a `realPath`. |

Cada subject tiene su propio `results/input/{subject}/config.json` que hace **merge profundo** sobre el global (claves del subject sobrescriben las globales).

> **Rutas de directorio:** todos los módulos del pipeline deben resolver rutas a través de `processControl.env` (poblado por `manageEnv()` en `paramsManager.py`). Está prohibido hardcodear `"results"` o `"results/input"` en rutas de archivos. Si se necesita la base de `results/`, usar `processControl.env.get("results_root", "results")`.

---

## 4. Orden de ejecución del pipeline (`mainProcess()`)

| Paso | Módulo | Toma (input) | Deja (output) | Fase |
|---|---|---|---|---|
| 1 | `normalizeSearchResults` | `conceptsQuery.json` + exports o APIs | `*_search.json` + snapshots en `_raw/` | Local (start) |
| 2 | `searchMergeEngine` | `*_search.json` | `canonical.json` (union-find: pmid → DOI → título+año → título solo; `global_id` como `doi:<doi>` o `title:<sha1>`) | Local (start) |
| 3 | `rankingEngine` | `canonical.json` + `studyDescription.json` | `ranked_papers.json` | Local (start) |
| 4 | `enrichmentEngine` | `ranked_papers.json` | `selected_papers.json`, `review_original.pdf`, `review_es.pdf`, `selection_checkpoint.json` | Local (start) |
| 5 | `manualPapersScanner` | PDFs en `manual_papers/` | `manual_papers.json` (`origin` = manual o manuscript) | Local (start) |
| 5b | `manualIngestion` | `manual_papers.json` + `selected_papers.json` | `selected_papers.json` actualizado | Local (start) |
| 6 | `adquisitionEngine` | `selected_papers.json` | PDFs en `pdfs/`, `selected_acquired.json`, `pdf_download_log.json`, `missing_pdfs.json`, `pdf_inventory.json` | Local (start) |
| extra | `completeSelection` | — | copia PDFs a `selected_papers/` y genera `prisma.md` | Local (start) |
| 7 | `corpusCleaning` | `selected_acquired.json` + PDFs + `pdf_download_log.json` | `papers_metadata.json`, `papers_text.json` (texto limpio + secciones + `figures_tables_captions`) | Local (start) |
| 8 | `argumentativeZoning` | `papers_text.json` | `papers_zoned.json` (párrafos: CLAIM, EVIDENCE, METHOD, BACKGROUND, GAP, FUTURE_WORK, REST) | Servidor (server) |
| 9 | `structureExtraction` | `papers_text.json` | `papers_structured.json` (contribuciones, metodología, resultados, métricas, datasets...) + `llm_cache.json` | Servidor (server) |
| 10 | `discoveryEngine` | `papers_text.json` + `studyDescription.json` + `conceptsQuery.json` | `candidate_concepts.json`, `candidate_concepts_by_rq.json` (usa modelo base o fine-tuneado) | Servidor (server) |
| 11 | `conceptMiningEngine` | `candidate_concepts.json` + `papers_text.json` + `papers_metadata.json` | `concept_candidates.json` (keywords con inyección de términos estructurados + dedup semántica) | Servidor (server) |
| 12 | `conceptAlignment` | `concept_candidates.json` + `conceptsQuery.json` | `aligned_concepts.json` | Servidor (server) |
| 13 | `fineTuningEngine` | `aligned_concepts.json`, `papers_zoned.json`, `papers_structured.json` | `models/slip-{subject}/` (auto-detección por checksum SHA-256; **no relanza el pipeline automáticamente**; avisa al final si se regeneró para que el operador lo aproveche en una pasada posterior) | Servidor (server) |
| 14 | `conceptEvidence` | `aligned_concepts.json` + `papers_text.json` + config | `concept_evidence.json` (evidencias diferenciales + bloque consenso) | Servidor (server) |
| 15 | `clusterEvidences` | `concept_evidence.json` | `clustered_evidences.json` | Servidor (server) |
| 16 | `enrich_evidences` | `clustered_evidences.json` + `papers_text.json` | `enriched_evidences.json` | Servidor (server) |
| 17 | `sythesizeFindings` | `enriched_evidences.json` + entradas | `concept_findings.json` (+ `narrative_summaries.json`) | Servidor (server) |
| 18 | `technicalAnnex` | `concept_findings.json` + `aligned_concepts.json` + corpus | `technical_annex.json` | Servidor (server) |
| 19 | `trainingMaterials` | varios JSONs + entradas | `training_materials.md` (+ `manuscript_context_cache.json` si aplica) | Servidor (server) |
| 20 | `relatedWork` | `concept_findings.json` + `papers_metadata.json` | `related_work.md` | Servidor (server) |
| 21 | `geminiNotebookExport` | `selected_acquired.json`, `ranked_papers.json`, `papers_text.json` | `notebook/gemini_notebook_NN.md` (máx. 50 papers/archivo, 200 MB/archivo, 500k palabras/paper) | Servidor (server) |

**`chatEngine`** (RAG sobre literatura) **no forma parte del pipeline secuencial**. Se usa aparte como módulo o app Streamlit: `streamlit run sources/chatEngine.py -- --ui`.

---

## 5. Conceptos clave

- **`paper_id`**: identificador por fuente (`wos_1`, `scopus_42`, `ieee_7`, `pubmed_3`…).
- **`global_id`**: identidad unificada tras el merge. Formato `doi:<doi>` o `title:<sha1>`. Usado por la bóveda de PDFs (`_pdf_vault`).
- **Section-weighted embeddings**: pesos por sección (`technical_paper` / `evidence_scoring`) que favorecen title/methodology/results/conclusion y atenúan introducción/fondo.
- **Argumentative zoning**: 7 zonas retóricas con pesos híbridos en evidencias (CLAIM 2.0 > EVIDENCE 1.8 > METHOD 1.2 > GAP 0.8 > BACKGROUND 0.3 > REST 0.0).
- **Consenso vs. diferencial**: ventana con genericidad alta (se repite en muchos papers) va al bloque *"Consenso general (ya conocido)"*; el resto son *knowledge units* diferenciales. Evita que lo genérico contamine lo singular en `training_materials.md`.
- **Fine-tuning automático**: `fineTuningEngine` genera pares CLAIM↔EVIDENCE, RQ↔Paper, Concepto↔Paper sin etiquetado manual. Guarda el modelo en `results/input/{subject}/models/slip-{subject}/`. Si el corpus no cambió (checksum SHA-256 de `conceptsQuery.json`, `papers_structured.json`, `aligned_concepts.json`, `papers_zoned.json`), se reutiliza; **si se regenera un modelo nuevo, no se relanza el pipeline automáticamente**. Se muestra un aviso al final de la ejecución indicando que el modelo estará disponible para `discoveryEngine` en una pasada posterior, y el operador relanzará cuando considere oportuno.
- **Blacklist** (`personalizacion.json`): IDs a excluir antes de descargar PDFs y al generar `training_materials.md` (no aparecen en PRISMA, bibliografía ni hallazgos).

---

## 6. Puntos de interacción manual (pipeline semisecuencial)

1. **Selección de papers** (`enrichmentEngine`): se generan `review_original.pdf` / `review_es.pdf`; el investigador acepta/rechaza candidatos. Progreso en `selection_checkpoint.json` (reanudable).

   **Regla del checkpoint:** solo se reutiliza si las condiciones de entrada son idénticas (mismo conjunto de `paper_ids` en `ranked_papers.json`, mismo orden). Si el ranking cambió (nuevas estrategias, nuevas queries, diferentes papers), el checkpoint se descarta automáticamente y la selección empieza desde cero. Esto evita arrastrar decisiones de una ejecución anterior sobre un conjunto diferente de papers.
2. **Ingestión manual** (`manualIngestion`): PDFs semilla en `manual_papers/` se integran en `selected_papers.json`.
3. **Descarga de PDFs** (`adquisitionEngine`): si falla una descarga, interfaz de recuperación manual. El usuario deja el PDF en `results/output/{subject}/pdfs/manual/`. Los PDFs se archivan en `_pdf_vault/`.

---

## 7. Iconos en writeLog para logs sin color

La función `writeLog()` en `sources/common/common.py` prepende iconos visuales a los mensajes según el nivel:

- `WARNING` → `⚠️  <mensaje>`
- `ERROR` → `❌  <mensaje>`
- Otros niveles → sin cambios

Esto facilita la lectura en ficheros `sbatch out` del servidor donde no hay colores de terminal.

---

## 7. Ejecución y dependencias

### Ejecución completa en local (comportamiento por defecto)
```bash
source .venv/bin/activate

# Pipeline completo para el subject "sensores"
python main.py --subject sensores --proc SLIP

# Con manuscrito del investigador y sin desarrollar conceptos emergentes
python main.py --subject sensores --proc SLIP --manuscript 1 --emergente 0
```

### Ejecución dividida local ↔ servidor GPU
El pipeline puede dividirse en dos fases para ejecutar la parte interactiva en local
y la parte intensiva en GPU en un servidor remoto:

```bash
# 1. En local: parte interactiva (pasos 1-7: normalización hasta limpieza de corpus)
#    Requiere intervención del investigador (selección de papers, resolver descargas PDF).
python main.py --subject sensores --proc SLIP --serverGPU start

#    → Subir el directorio results/ al servidor GPU

# 2. En servidor GPU: parte no interactiva (pasos 8-21: zoning hasta exportación Gemini Notebook)
python main.py --subject sensores --proc SLIP --serverGPU server
```

Si `--serverGPU` no se indica, se ejecuta el pipeline completo en local (comportamiento habitual).

Flags:
- `--subject`: carpeta en `results/input/` (default `sensores`).
- `--proc`: `SLIP` (pipeline completo). Otro valor ejecuta `customProcess()`.
- `--manuscript 1`: integra `manual_papers/manuscript/` y añade sección de evidencias del manuscrito.
- `--emergente 0`: no desarrolla conceptos emergentes en `training_materials.md`.
- `--serverGPU start`: ejecuta solo la parte interactiva local (pasos 1-7) y se detiene.
- `--serverGPU server`: ejecuta solo la parte no interactiva en servidor GPU (pasos 8-21).
- `--step <nombre>`: inicia el pipeline directamente desde el paso indicado y continúa hasta el final.
  Ejemplos: `--step TechnicalAnnex`, `--step DiscoveryEngine`, `--step StructureExtraction`.
  Si no se indica, se ejecuta normalmente (o según `--serverGPU`).
  Pasos disponibles: NormalizeSearchResults, SearchMergeEngine, RankingEngine, EnrichmentEngine,
  ManualPapersScanner, ManualIngestion, AdquisitionEngine, CompleteSelection, CorpusCleaning,
  ArgumentativeZoning, StructureExtraction, DiscoveryEngine, ConceptMiningEngine, ConceptAlignment,
  FineTuningEngine, ConceptEvidence, ClusterEvidences, EnrichEvidences, SythesizeFindings,
  TechnicalAnnex, TrainingMaterials, RelatedWork, GeminiNotebookExport.

Reanudar tras suspensión o bloqueo (`--step`):
  Si el equipo se suspende o el pipeline se queda esperando a Ollama, usa `--step` para
  reanudar desde el paso donde se quedó. Ejemplo: si se cortó en technicalAnnex:
    python main.py --subject sensores --proc SLIP --step TechnicalAnnex
  Esto saltará los pasos ya completados y continuará desde TechnicalAnnex hasta el final.

Timeout de llamadas LLM:
  Cada llamada a Ollama tiene un timeout de 120 segundos (configurable en
  `config.json > defaults.llm.llm_timeout`). Si una llamada excede este tiempo,
  se lanza una excepción y el paso actual falla en lugar de quedarse bloqueado
  indefinidamente. Esto evita que una suspensión del equipo deje al pipeline
  colgado para siempre.

Dependencias externas:
- **Python 3.10+** (desarrollado con 3.12).
- **Ollama** en `localhost:11434` con `qwen3:8b`, `qwen2.5:7b`, `llama3.1:8b`.
- **Token de Hugging Face** (embeddings `BAAI/bge-*`).
- **Modelos spaCy**: `en_core_web_sm`, `es_core_news_sm`.
- **GPU CUDA** recomendada (≥ 8 GB VRAM); en CPU funciona pero es lento.
- **IEEE API key**: configurada en `config.json > defaults.search.providers.ieee.api_key`. El modo está en `"api"` (búsquedas vía API de IEEE Xplore) en lugar de `"file"` (archivos CSV exportados manualmente).
- **ACL Anthology**: nuevo proveedor (`sources/ingestion/acl_anthology_provider.py`) que usa la librería `acl_anthology` para iterar sobre el repositorio git de ACL Anthology (clonado en `sources/third_party/acl-anthology`, compartido por todas las investigaciones) y filtrar papers por query booleana (AND/OR con paréntesis) y año mínimo. Configurar en `config.json > defaults.search.providers.acl_anthology` con `mode: "api"`, `repo_path` (opcional, por defecto `sources/third_party/acl-anthology`), `min_year` y `max_results`. Añadir `acl_anthology` a `search.availables` y a `requirements.txt` (`acl-anthology`, `gitpython`). Si el repositorio no existe, el proveedor intenta clonarlo automáticamente. El directorio `sources/third_party/` está excluido de git (`.gitignore`).
- **Paper Blacklist con indicadores numéricos**: `config.json > defaults.paper_blacklist.entries` acepta lista de entradas con `id`, `reason` y `detail`. Motivos: `low_text` (→ indicador 1: bajo contenido tras limpieza), `operator_rejected` (→ indicador 2: excluido de selección manual por el operador), `pdf_unreadable`, etc. Se alimenta automáticamente desde `corpusCleaning` (low_text) y `enrichmentEngine` (operator_rejected). Los papers blacklisteados se excluyen antes del ranking en `rankingEngine` y se reportan en `prisma.md` sección 6.6 usando indicadores numéricos con leyenda al final y título del paper.
- **Distribución por venue en PRISMA basada en papers > umbral**: La sección **6.5** de `prisma.md` cuenta venues sobre todos los papers que SUPERAN el umbral de similitud (ranked_papers.json + ranking_outside_top_k.json), no solo sobre los seleccionados manualmente. Se distingue entre papers dentro de top_k y papers fuera de top_k pero > umbral. Tabla con columnas: Venue / Conferencia | Papers > umbral | Papers en top_k. Esto hace que el PRISMA sea más representativo de la investigación al incluir todos los papers relevantes, no solo los seleccionados manualmente.
  - Las venues objetivo son **configurables por investigación** en `results/input/{subject}/conceptsQuery.json` bajo las claves `prisma_target_venues` (lista de códigos de venue) y `prisma_venue_aliases` (mapeo de código → nombres alternativos para mejor matching). El matching usa subcadenas del código y de sus aliases, case-insensitive.
  - Además, se soporta identificación de venue por **patrón de DOI** mediante la clave `doi_venue_patterns` en `conceptsQuery.json`. Cada entrada tiene `pattern` (patrón a buscar en el venue component del DOI), `venue` (nombre del venue) y `category` (categoría). Esto permite detectar venues incluso cuando el campo `venue` del paper está vacío (común en ACL Anthology). El matching verifica si el patrón está contenido en el venue component del DOI (ej. `acl-long` en `2023.acl-long.741` → ACL Main (Long)).

Tests:
```bash
python -m unittest discover -s tests -t .
```
Cubren normalización de DOI, `pdfIndex`, filtro de referencias, cita a nivel de afirmación, dedup de conceptos, OpenAlex, anexo técnico, genericidad/consenso.

---

## 8. Estado actual (SESSION_STATE.md, 2026-10-23)

Última ejecución: **completada exitosamente** hasta `completeSelection` (`prisma.md` generado con venues objetivo mejoradas).

Mejoras implementadas (históricas + esta sesión):
- Section-weighted embeddings en `discoveryEngine` (+6 topics, +4 docs).
- Config unificada con `section_weights.presets` y `section_aliases`.
- `structureExtraction` (71 papers, campos LLM: contribuciones, metodología, resultados, métricas, limitaciones, future_work…).
- `conceptMiningEngine` con inyección de términos estructurados (36.7% keywords estructurados, 345 métricas, 101 sensores, 45 datasets).
- `argumentativeZoning` (2.788 párrafos clasificados en 7 zonas).
- Fine-tuning de embeddings por investigación con auto-detección por checksum.
- Captions de figuras/tablas como sección virtual `figures_tables_captions` (peso 1.8).
- Caché LLM persistente en `structureExtraction` (`llm_cache.json`).
- Múltiples estrategias de búsqueda (`query_strategies` en `conceptsQuery.json`).
- `chatEngine` (RAG sobre literatura, Streamlit).
- Límite de tópicos emergentes (`max_emerging_topics`, default 20).
- Nuevo proveedor **ACL Anthology** (`sources/ingestion/acl_anthology_provider.py`) con librería `acl_anthology`, registro en `manager.py`, y auto-clone del repositorio en `sources/third_party/acl-anthology/`.
- **Venues configurables por investigación** en `conceptsQuery.json`: `prisma_target_venues`, `prisma_venue_aliases` y `doi_venue_patterns`.
- **Venue por patrón de DOI**: el proveedor extrae `venue_from_doi` y `venue_category` del DOI, se propaga a través del merge y se usa como fallback en el PRISMA.
- PRISMA sección 6.5: de 3 papers con venue objetivo (solo venue field) a **29 papers** (venue + venue_from_doi).
- Umbral de similitud 0.72: 409 papers > umbral, suficientes para 130 top_k.
- `.gitignore`: `sources/third_party/` excluido de git.

---

## 9. Cómo usar este archivo en futuras sesiones

Al iniciar una sesión nueva, pega el contenido de este archivo (`AGENTS.md`) junto con `SESSION_STATE.md` como contexto inicial al agente. Así evitas el proceso de aprendizaje inicial.

Cuando modifiques código, configuración o flujos, **actualiza `AGENTS.md` y `SESSION_STATE.md`** al terminar la sesión para mantener la memoria sincronizada.

---

## 10. Próximos pasos sugeridos

Revisa `SESSION_STATE.md` para ver la última ejecución y las tareas pendientes. Si no hay ninguna acción definida, puedes:
1. Ejecutar el pipeline completo: `python main.py --proc SLIP`
2. Depurar o ampliar algún módulo de `sources/`
3. Ajustar `config.json` o la config por subject
4. Añadir o corregir tests en `tests/`
5. Trabajar en `chatEngine` / Streamlit
