# Pipeline SLIP — Documentación de flujo

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Pipeline](https://img.shields.io/badge/Pipeline-SLIP-4A90D9)
![Status](https://img.shields.io/badge/Documentation-Current-brightgreen)
![License](https://img.shields.io/badge/License-MIT-green)

> **SLIP** (Scientific Literature Intelligence Pipeline): pipeline secuencial que, a partir de
> resultados de búsqueda bibliográfica, produce un corpus limpio, evidencia estructurada por
> preguntas de investigación y documentos de síntesis (trabajo relacionado, materiales de formación).

## 📖 1. Visión general

El pipeline se ejecuta como un **proceso secuencial**: cada módulo consume los ficheros de salida del
módulo anterior y deja los suyos para el siguiente. La secuencia exacta está orquestada en
`main.py` (`mainProcess()`).

### Guía del proceso

El flujo se configura con dos ficheros por "asunto" (subject) y uno global:

| Fichero | Ubicación | Papel |
|---|---|---|
| `config.json` | raíz del proyecto | Configuración global: fuentes de búsqueda disponibles, modelos LLM/embeddings, umbrales de los módulos. |
| `studyDescription.json` | `results/input/{subject}/` | Define el estudio: objetivos, keywords, `focus_terms` y campos de extracción técnica. Guía ranking, minería y anexo técnico. |
| `conceptsQuery.json` | `results/input/{subject}/` | Define las preguntas de investigación (conceptos) y el query de búsqueda. Guía descubrimiento, alineación y síntesis. |

### Convenciones de directorios

- **Entrada**: `results/input/{subject}/` — ficheros de búsqueda exportados y ficheros guía.
- **Salida**: `results/output/{subject}/` — JSONs intermedios, PDFs adquiridos y documentos finales.
- Los ficheros de búsqueda se nombran según `config.json` (`wos_export.txt`, `scopus_export.csv`,
  `ieee_export.csv`, `pubmed_export.txt`).

---

## 🔄 2. Diagrama secuencial

```
[exports: wos/scopus/ieee/pubmed]
        │
        ▼
1. normalizeSearchResults ──► *_search.json
        │
        ▼
2. searchMergeEngine ────────► canonical.json
        │
        ▼
3. rankingEngine ────────────► ranked_papers.json
        │
        ▼
4. enrichmentEngine ─────────► selected_papers.json + review_*.pdf
        │
        ▼
5. manualIngestion (opcional)► selected_papers.json (actualizado)
        │
        ▼
6. adquisitionEngine ────────► PDFs + selected_acquired.json + logs
        │
        ▼
7. corpusCleaning ───────────► papers_metadata.json + papers_text.json
        │
        ▼
8. discoveryEngine ──────────► candidate_concepts.json
        │
        ▼
9. conceptMiningEngine ──────► concept_candidates.json
        │
        ▼
10. conceptAlignment ────────► aligned_concepts.json
        │
        ▼
11. conceptEvidence ─────────► concept_evidence.json
        │
        ▼
12. clusterEvidences ────────► clustered_evidences.json
        │
        ▼
13. enrich_evidences ────────► enriched_evidences.json
        │
        ▼
14. sythesizeFindings ───────► concept_findings.json
        │
        ├──► 15. technicalAnnex ─► technical_annex.json
        ├──► 16. trainingMaterials ─► training_materials.md
        └──► 17. relatedWork ─► related_work.md
```

---

## 🧩 3. Módulos (toma → deja)

### 1. Normalización de resultados de búsqueda
**Módulo:** `sources/normalizeSearchResults.py` · `processNormalizeSearchResults()`

**Propósito (alto nivel):** Lee los ficheros exportados de las bases de datos bibliográficas y los
normaliza a un formato JSON común, asignando un `paper_id` por fuente (`wos_1`, `scopus_42`,
`ieee_7`, `pubmed_3`, ...). Soporta múltiples ficheros por fuente mediante glob.

- **Toma:** `results/input/{subject}/wos_export.txt`, `scopus_export.csv`, `ieee_export.csv`, `pubmed_export.txt` (formato MEDLINE en el caso de PubMed).
- **Deja:** `results/output/{subject}/wos_search.json`, `scopus_search.json`, `ieee_search.json`, `pubmed_search.json`.

### 2. Fusión de fuentes
**Módulo:** `sources/searchMergeEngine.py` · `processSearchMergeEngine()`

**Propósito:** Fusiona y deduplica los papers de todas las fuentes configuradas en
`config.json > search.availables`, identificando duplicados por `global_id` (DOI o título
normalizado) y conservando los `paper_id` originales de cada fuente.

- **Toma:** `{wos|scopus|ieee|pubmed}_search.json` (los ficheros del paso 1).
- **Deja:** `canonical.json`.

### 3. Ranking de papers
**Módulo:** `sources/rankingEngine.py` · `processRankingEngine()`

**Propósito:** Construye un query de relevancia a partir de `studyDescription.json` y ordena el
corpus canónico por afinidad temática (relevancia) para seleccionar el subconjunto de interés.

- **Toma:** `canonical.json` + `studyDescription.json` (entrada).
- **Deja:** `ranked_papers.json`.

### 4. Enriquecimiento y selección de papers
**Módulo:** `sources/enrichmentEngine.py` · `processEnrichmentEngine()`

**Propósito:** Enriquece cada paper con metadatos adicionales (p. ej. citas vía API), construye una
revisión de candidatos y genera dos PDFs de revisión (original y en español) para que el
investigador seleccione manualmente (o por checkpoint) los papers finales.

- **Toma:** `ranked_papers.json`.
- **Deja:** `selected_papers.json`, `review_original.pdf`, `review_es.pdf`, `selection_checkpoint.json`.
  - *Nota:* los comentarios de `main.py` mencionan `enriched_papers.json` y `candidate_review.json`,
    pero la implementación actual no los persiste.

### 5. Ingestión manual (opcional)
**Módulo:** `sources/manualIngestion.py` · `processManualIngestion()`

**Propósito:** Puente para incorporar PDFs "semilla" externos al flujo: asigna `paper_id`/`global_id`
y los fusiona en el conjunto seleccionado. Si no existe `manual_papers.json`, el módulo se omite.

- **Toma:** `results/input/{subject}/manual_papers/manual_papers.json` + PDFs de esa carpeta; `selected_papers.json`.
- **Deja:** `selected_papers.json` (actualizado con los papers manuales).

### 6. Adquisición de PDFs
**Módulo:** `sources/adquisitionEngine.py` · `processAcquisitionEngine()`

**Propósito:** Descarga el texto completo (PDF) de cada paper seleccionado resolviendo DOI/URLs,
con intervención manual para recuperaciones fallidas y una "bóveda" de PDFs (`_pdf_vault`) para
reutilizar descargas ya hechas.

- **Toma:** `selected_papers.json`.
- **Deja:** PDFs en `results/output/{subject}/pdfs/`, `selected_acquired.json`, `pdf_download_log.json`,
  `missing_pdfs.json`, `pdf_inventory.json` (índice de la bóveda).

### 7. Limpieza de corpus
**Módulo:** `sources/corpusCleaning.py` · `processCorpusCleaning()`

**Propósito:** Extrae el texto de los PDFs (vía el servicio federado `fullTextExtractionEngine`,
que excluye el contenido de tablas), aplica filtros de calidad (mínimo de palabras/caracteres) y una
limpieza regex (URLs, emails, corte en bibliografía, números de página, etc.). Produce los contratos
de texto y metadatos que alimentan toda la fase de análisis.

- **Toma:** `selected_acquired.json` (y los PDFs en disco).
- **Deja:** `papers_metadata.json` (metadatos + estadísticas post-limpieza) y `papers_text.json`
  (texto limpio + secciones).

### 8. Descubrimiento
**Módulo:** `sources/discoveryEngine.py` · `processDiscoveryEngine()`

**Propósito:** Descubrimiento guiado por las preguntas de investigación: identifica qué papers del
corpus son relevantes para cada RQ (concepto) y produce candidatos concepto→paper.

- **Toma:** `papers_text.json` + `studyDescription.json` y `conceptsQuery.json` (entrada).
- **Deja:** `candidate_concepts.json` (+ `candidate_concepts_by_rq.json`).

### 9. Minería de conceptos
**Módulo:** `sources/conceptMiningEngine.py` · `processConceptMiningEngine()`

**Propósito:** Para cada candidato, extrae keywords y el fragmento de texto relevante de cada paper,
indicando la sección de la que proviene.

- **Toma:** `candidate_concepts.json` + `papers_text.json` + `papers_metadata.json` (y `focus_terms` de `studyDescription.json`).
- **Deja:** `concept_candidates.json`.

### 10. Alineación de conceptos
**Módulo:** `sources/conceptAlignment.py` · `processConceptAlignment()`

**Propósito:** Alinea los conceptos candidatos con las preguntas de investigación definidas en
`conceptsQuery.json`, quedándose con los más relevantes.

- **Toma:** `concept_candidates.json` + `papers_text.json` + `papers_metadata.json` + `conceptsQuery.json` (entrada).
- **Deja:** `aligned_concepts.json`.

### 11. Extracción de evidencias
**Módulo:** `sources/conceptEvidence.py` · `processConceptEvidence()`

**Propósito:** Extrae evidencias (párrafos) de los papers seleccionados para cada concepto/RQ
alineado, configurable desde `studyDescription.json`.

- **Toma:** `aligned_concepts.json` + `papers_text.json` + `studyDescription.json` (config de evidencias).
- **Deja:** `concept_evidence.json`.

### 12. Clustering de evidencias
**Módulo:** `sources/clusterEvidences.py` · `processClusterEvidences()`

**Propósito:** Agrupa las evidencias en clusters temáticos dentro de cada concepto para estructurar
el análisis posterior.

- **Toma:** `concept_evidence.json`.
- **Deja:** `clustered_evidences.json`.

### 13. Enriquecimiento de evidencias
**Módulo:** `sources/enrich_evidences.py` · `processEnrichEvidences()`

**Propósito:** Enriquece cada cluster/evidencia con análisis estructurado generado por LLM
(limitaciones, lagunas de investigación, trabajo futuro, uso, etc.), usando el texto por secciones
de cada paper.

- **Toma:** `clustered_evidences.json` + `papers_text.json` (+ config de `studyDescription.json`).
- **Deja:** `enriched_evidences.json`.

### 14. Síntesis de hallazgos
**Módulo:** `sources/sythesizeFindings.py` · `processSynthesizeFindings()`

**Propósito:** Sintetiza, por cada pregunta de investigación, los hallazgos del estado del arte a
partir de las evidencias enriquecidas. Utiliza caché de resúmenes narrativos (`narrative_summaries.json`).

- **Toma:** `enriched_evidences.json` + `studyDescription.json` y `conceptsQuery.json` (entrada).
- **Deja:** `concept_findings.json`.

### 15. Anexo técnico
**Módulo:** `sources/technicalAnnex.py` · `processTechnicalAnnex()`

**Propósito:** Extrae de forma estructurada los campos técnicos definidos en
`studyDescription.json > project.technical_extraction.fields` (modalidades, métricas, datasets,
limitaciones, contribuciones, ...) a partir de los hallazgos y del corpus.

- **Toma:** `concept_findings.json` + `aligned_concepts.json` + `papers_text.json` + `papers_metadata.json` + `studyDescription.json`.
- **Deja:** `technical_annex.json`.

### 16. Materiales de formación
**Módulo:** `sources/trainingMaterials.py` · `processTrainingMaterials()`

**Propósito:** Genera un documento de materiales de aprendizaje en Markdown a partir de los hallazgos
y del corpus, con traducción configurable (caché en `translation_cache.json`).

- **Toma:** `concept_findings.json` + `candidate_concepts.json` + `aligned_concepts.json` +
  `technical_annex.json` + `papers_text.json` + `papers_metadata.json` + `conceptsQuery.json` + `studyDescription.json`.
- **Deja:** `training_materials.md`.

### 17. Trabajo relacionado
**Módulo:** `sources/relatedWork.py` · `processRelatedWork()`

**Propósito:** Genera un documento de *related work* (trabajo relacionado) sintetizando los hallazgos
por pregunta de investigación, con los DOI/metadatos de los papers involucrados.

- **Toma:** `concept_findings.json` + `papers_metadata.json` (DOIs).
- **Deja:** `related_work.md`.

---

## 📝 4. Notas

- **Extracción de texto:** el módulo `fullTextExtractionEngine.py` es un *servicio* reutilizable
  (no un paso del pipeline) usado por `corpusCleaning`. El `processFullTextExtraction` que aparece
  comentado en `main.py` está deprecado: la extracción se integra en el paso 7.
- **Puntos de interacción manual:** paso 4 (selección interactiva), paso 5 (ingestión manual de
  PDFs) y paso 6 (recuperación manual de PDFs fallidos).
- **Fuentes de búsqueda:** la lista activa se controla con `config.json > search.availables`
  (`wos`, `scopus`, `ieee`, `pubmed`).