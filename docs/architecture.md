# 🏗️ Arquitectura del Pipeline SLIP

Este documento describe el flujo de datos entre módulos, qué consume y produce cada etapa, y cómo se integran los módulos nuevos (fine-tuning, zoning con LLM refiner, captions de tablas/figuras, fine-tuning de embeddings).

## Flujo general

```
┌─────────────────────────────────────────────────────────────────┐
│  ENTRADA: results/input/{subject}/                             │
│  - studyDescription.json                                       │
│  - conceptsQuery.json                                          │
│  - *_search.json (WOS, Scopus, IEEE, PubMed, ACM, arXiv)       │
│  - config.json (específico del subject)                        │
│  - manual_papers/ (PDFs semilla)                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  1. searchMergeEngine    → canonical.json (papers unificados)   │
│  2. rankingEngine        → ranked_papers.json                   │
│  3. enrichmentEngine     → selected_papers.json + review pdfs   │
│  4. manualPapersScanner  → integra manual_papers/               │
│  5. manualIngestion      → une PDFs semilla                     │
│  6. adquisitionEngine    → PDFs + pdf_download_log.json         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  7. corpusCleaning     → papers_metadata.json                   │
│                      → papers_text.json (clean_text +           │
│                         clean_sections +                          │
│                         figures_tables_captions*)               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  8. argumentativeZoning  → papers_zoned.json (párrafos con zona │
│                      CLAIM/EVIDENCE/METHOD/BACKGROUND/GAP/      │
│                      FUTURE_WORK/REST + confidence)             │
│     [*Llm refiner opcional para baja confianza*]                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  9. structureExtraction  → papers_structured.json (contrib.,   │
│                      metodología, resultados, métricas, etc.)   │
│     [*Cache LLM persistente cross-run*]                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 10. fineTuningEngine     → models/slip-{subject}/               │
│                      (modelo de embeddings fine-tuneado por     │
│                       investigación; checksum para detectar     │
│                       cambios en el corpus)                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 11. discoveryEngine      → candidate_concepts.json             │
│                      (usa section-weighted + modelo           │
│                       fine-tuneado si existe)                   │
│     [*Section-weighted embeddings + section filter*]            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 12. conceptMiningEngine  → concept_candidates.json             │
│                      (keywords con sección, métricas, datasets, │
│                       sensores + dedup semántica)               │
│     [*Inyección directa de términos estructurados*]              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 13. conceptAlignment     → aligned_concepts.json               │
│                      (papers asignados a conceptos/topics)      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 14. conceptEvidence      → concept_evidence.json               │
│                      (knowledge units con sección + zona +      │
│                       scoring híbrido sección×zona)             │
│     [*Pesos por zona argumentativa*]                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 15. clusterEvidences     → clustered_evidences.json            │
│ 16. enrich_evidences     → enriched_evidences.json             │
│ 17. synthesizeFindings   → concept_findings.json               │
│ 18. technicalAnnex       → technical_annex.json                │
│ 19. trainingMaterials    → training_materials.md               │
│     [*max_emerging_topics limita tópicos LLM*]                  │
│ 20. relatedWork          → related_work.md                     │
│ 21. chatEngine            → chat con RAG sobre literatura       │
│     [*Preguntas en lenguaje natural sobre papers y hallazgos*]  │
└─────────────────────────────────────────────────────────────────┘
```

* `*` indica novedad en v2.0.

---

## Módulos y responsabilidades

### corpusCleaning
Extrae texto limpio de los PDFs, divide en secciones y **añade `figures_tables_captions`** como sección virtual con los captions de tablas y figuras (solo texto descriptivo, no contenido celular ruidoso). Peso 1.8 en ambos presets de section_weights.

### argumentativeZoning
Clasifica cada párrafo en una zona argumentativa (CLAIM, EVIDENCE, METHOD, BACKGROUND, GAP, FUTURE_WORK, REST) usando reglas heurísticas. Opcionalmente, el **LLM refiner** reclasifica párrafos con confianza baja (< 0.6).

### structureExtraction
Extrae información estructurada por paper (contribuciones, metodología, resultados, limitaciones, etc.) usando LLM. **Cache LLM persistente**: guarda respuestas en `llm_cache.json` para evitar re-llamadas entre runs.

### fineTuningEngine
Genera pares de entrenamiento automáticamente (sin etiquetado manual) a partir de:
- papers_zoned.json (CLAIM ↔ EVIDENCE del mismo paper)
- aligned_concepts.json + papers_structured.json (concepto ↔ paper)
- conceptsQuery.json (RQ ↔ paper)

Fine-tunea `BAAI/bge-base-en-v1.5` y guarda el modelo en `results/input/{subject}/models/slip-{subject}/`. Usa un **checksum SHA-256** de los archivos del corpus para decidir si regenerar o reutilizar.

### discoveryEngine
Descubre topics por RQ usando section-weighted embeddings. Usa el modelo fine-tuneado si existe (de lo contrario `bge-base-en-v1.5` base).

### conceptMiningEngine
Extrae keywords por tema con KeyBERT, inyección directa de términos estructurados (métricas, datasets, sensores, baselines) y **deduplicación semántica** (coseno ≥ 0.85 elimina duplicados).

---

## Directorios de salida

`results/output/{subject}/`

| Fichero | Descripción |
|---|---|
| `canonical.json` | Papers unificados de todas las fuentes |
| `ranked_papers.json` | Papers rankeados por relevancia |
| `selected_papers.json` | Papers seleccionados tras revisión |
| `papers_metadata.json` | Metadatos de papers |
| `papers_text.json` | Texto limpio + secciones + figures_tables_captions |
| `papers_zoned.json` | Párrafos con zona argumentativa |
| `papers_structured.json` | Info estructurada por paper (LLM) |
| `candidate_concepts.json` | Topics descubiertos por RQ |
| `concept_candidates.json` | Keywords por concepto |
| `aligned_concepts.json` | Papers asignados a conceptos |
| `concept_evidence.json` | Evidencias por concepto |
| `clustered_evidences.json` | Evidencias clusterizadas |
| `enriched_evidences.json` | Evidencias enriquecidas |
| `concept_findings.json` | Hallazgos sintetizados |
| `technical_annex.json` | Perfiles técnicos |
| `training_materials.md` | PRISMA + hallazgos + tópicos emergentes |
| `related_work.md` | Related Work listo para paper |
| `llm_cache.json` | Cache de respuestas LLM (structureExtraction) |
| `models/slip-{subject}/` | Modelo de embeddings fine-tuneado |

---

## Configuración por investigación

Cada subject tiene su propio `config.json` en `results/input/{subject}/config.json` que hace **merge profundo** con el `config.json` global. Las claves del config del subject sobrescriben las globales.

Ejemplo de uso de modelo fine-tuneado en discovery:

```
"discovery": {
    "embedding_model": "./models/slip-sensores",
    "embedding_model_dim": 768
}
```

---

## Flujo de fine-tuning (auto-detección)

1. Primera ejecución: `discoveryEngine` usa modelo base (`bge-base-en-v1.5`).
2. `fineTuningEngine` genera pares de entrenamiento y entrena el modelo.
3. Si se genera modelo nuevo, el pipeline se **reinicia automáticamente** (máximo 1 reinicio).
4. Segunda ejecución: `discoveryEngine` detecta y usa el modelo fine-tuneado existente.
5. Si el corpus cambia (checksum diferente), el fine-tuning se regenera.

---

## 💬 ChatEngine (Chat Científico con RAG)

El módulo `sources/chatEngine.py` permite al investigador hacer preguntas en lenguaje natural sobre los papers y hallazgos procesados por el pipeline SLIP.

### Arquitectura

```
┌─────────────────────────────────────────────────────────────────┐
│  Entrada: Papers estructurados + evidencias + hallazgos         │
│  - papers_text.json                                             │
│  - concept_evidence.json                                        │
│  - concept_findings.json                                        │
│  - papers_structured.json                                       │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  Chunking + Embeddings (BAAI/bge-base-en-v1.5)                  │
│  - Chunks de ~1200 caracteres                                   │
│  - Almacenamiento en memoria (numpy)                            │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  Consulta del usuario                                           │
│  → Embedding de la pregunta                                     │
│  → Similitud coseno con chunks                                  │
│  → Top-K chunks más relevantes                                  │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  Generación (Ollama + qwen3:8b)                                 │
│  - Prompt con contexto recuperado                               │
│  - Respuesta con citas de fuente                                │
└─────────────────────────────────────────────────────────────────┘
```

### Uso como módulo

```python
from sources.chatEngine import ChatEngine
from pathlib import Path

engine = ChatEngine(output_dir=Path("results/output/SensoresACL"))
engine.indexar()

resultado = engine.preguntar(
    "¿Qué modelos de lenguaje se comparan en los papers?"
)

print(resultado["respuesta"])
for fuente in resultado["fuentes"]:
    print(f"  [{fuente['paper_id']}] {fuente['titulo']} (sim: {fuente['similitud']})")
```

### Uso como aplicación web (Streamlit)

```bash
streamlit run sources/chatEngine.py -- --ui
```

### Características

- **Preguntas en lenguaje natural** sobre cualquier aspecto de los papers
- **Citas de fuente** con paper_id, sección y similitud
- **Reutiliza embeddings** existentes (no recalcula si ya están indexados)
- **Configurable**: `top_k` chunks, umbral de similitud, texto máximo por chunk
- **Compatible con el pipeline**: Usa los mismos modelos y datos ya generados

### Configuración

| Parámetro | Valor por defecto | Descripción |
|-----------|-------------------|-------------|
| `EMBEDDING_MODEL` | `BAAI/bge-base-en-v1.5` | Modelo de embeddings (mismo que el pipeline) |
| `OLLAMA_MODEL` | `qwen3:8b` | Modelo LLM para generación |
| `SIMILARITY_THRESHOLD` | `0.35` | Umbral mínimo de similitud para incluir un chunk |
| `TOP_K_CHUNKS` | `6` | Número de chunks recuperados por pregunta |
| `MAX_CHUNK_CHARS` | `1200` | Longitud máxima de cada chunk |
