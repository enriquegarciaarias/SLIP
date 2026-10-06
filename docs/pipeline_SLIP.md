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

### Flags de ejecución

| Flag | Valores | Efecto |
|---|---|---|
| `--manuscript` | `0` (def.) / `1` | `1` integra `manual_papers/manuscript/` en el circuito normal y añade la sección **"Evidencias en el manuscrito"** a `training_materials.md`. |
| `--emergente` | `0` / `1` (def.) | `0` no desarrolla los conceptos emergentes en `training_materials.md` (se conservan los conteos de PRISMA y la bibliografía). |

Ambos flags pueden fijarse también en `config.json > defaults.training_materials`
(claves `manuscript` y `emergente`); el argumento de línea de comandos tiene prioridad.

### Manuscrito del investigador (opcional)

Cuando `--manuscript 1`, el pipeline incorpora el manuscrito en construcción alojado en
`results/input/{subject}/manual_papers/manuscript/`:

```
manual_papers/manuscript/
├── manuscript.pdf     # manuscrito (obligatorio; activa el tratamiento y nunca se ingesta)
├── main.tex           # fuente LaTeX (opcional; de aquí se extraen los contextos de cita)
├── biblio.bib         # bibliografía BibTeX (opcional; mapea las citas con los PDFs)
└── <bibkey>.pdf       # PDFs asociados, nombrados con su clave BibTeX (recomendado)
```

- Los PDFs asociados se ingieren como `manual_papers` (quedan marcados con `origin="manuscript"`).
- Nombra cada PDF con la **bibkey** de `biblio.bib`: el mapeo es determinista. Como respaldo se
  intenta por DOI/título y por la bóveda `_pdf_vault`.
- Las referencias citadas que **ya están en el corpus** no necesitan PDF: se resuelven por DOI o
  título contra `papers_metadata.json`.

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
5. manualPapersScanner (opc.)► manual_papers.json (semilla + manuscrito)
        │
        ▼
5b. manualIngestion (opc.) ──► selected_papers.json (actualizado)
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

**Propósito (alto nivel):** Obtiene los resultados de las bases de datos y los normaliza a un
formato JSON común, asignando un `paper_id` por fuente (`wos_1`, `scopus_42`, `ieee_7`,
`pubmed_3`, ...). Cada fuente se ingesta en modo **API** o **fichero** según
`config.json > defaults.search.providers.<fuente>.mode`.

- **Modo API** (`sources/ingestion/`): consulta la API oficial (PubMed e-utilities, Scopus
  Search + Abstract Retrieval, IEEE Xplore Metadata, OpenAlex Works), guarda la respuesta cruda
  en `results/input/{subject}/_raw/{fuente}/` con un `fetch_manifest.json` (query, fecha, hash) y
  normaliza offline. Si la API falla o faltan credenciales, degrada a modo fichero.
- **Query decomposition** (opcional, `search.decomposition.enabled`): además de la query global de
  la fuente, lanza una consulta por pregunta de investigación (`conceptsQuery.json > concepts[]`)
  y deduplica los registros dentro de la fuente.
- **Modo fichero**: parsea los exports (`wos_export.txt`, `scopus_export.csv`, `ieee_export.csv`,
  `pubmed_export.txt`) y soporta múltiples ficheros por fuente mediante glob.

- **Toma (API):** `conceptsQuery.json` (claves `queries.<fuente>` o `query`) + credenciales en `config.json` (OpenAlex solo requiere `email`).
- **Toma (fichero):** `results/input/{subject}/wos_export.txt`, `scopus_export.csv`, `ieee_export.csv`, `pubmed_export.txt` (formato MEDLINE en el caso de PubMed).
- **Deja:** `results/output/{subject}/wos_search.json`, `scopus_search.json`, `ieee_search.json`, `pubmed_search.json` (+ `openalex_search.json` si está activo; snapshots crudos en `results/input/{subject}/_raw/`).

### 2. Fusión de fuentes
**Módulo:** `sources/searchMergeEngine.py` · `processSearchMergeEngine()`

**Propósito:** Fusiona y deduplica los papers de todas las fuentes configuradas en
`config.json > search.availables`, conservando los `paper_id` originales de cada fuente y
combinando los metadatos de todas ellas.

**Identidad (union-find sobre claves múltiples):** cada registro aporta varias claves y se
fusionan transitivamente los que comparten cualquiera de ellas, por orden de fiabilidad:

1. `pmid` (identidad unívoca PubMed).
2. DOI normalizado (sin prefijos `doi:`/`https://doi.org/`, sin puntuación final).
3. Título normalizado + año.
4. Título normalizado solo si es largo (≥ `search.merge.by_title_only_min_chars`) y no hay año.

Esto une duplicados que antes se perdían (p. ej. un registro PubMed con DOI erróneo y su gemelo
Scopus con DOI correcto se unen por título+año; variantes `…2018.3.008` / `…2018.03.008`
también). El `global_id` resultante mantiene el formato `doi:<doi>` / `title:<sha1>` para no
romper la bóveda de PDFs. La fusión es aditiva: autores, keywords, referencias, enlaces y
contadores se unen/maximizan en lugar de descartarse.

- **Toma:** `{wos|scopus|ieee|pubmed}_search.json` (los ficheros del paso 1).
- **Deja:** `canonical.json` (con `sources`, `source_indices`, `merged_paper_ids`, `pmids`, `dois`).

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

### 5. Escaneo y ingestión manual (opcional)
**Módulos:** `sources/manualPapersScanner.py` · `processManualPapersScanner()` y
`sources/manualIngestion.py` · `processManualIngestion()`

**Propósito:** Incorporar al circuito PDFs externos (semilla) y del manuscrito. El *scanner* recorre
los PDFs, extrae metadatos (DOI/título) y genera/actualiza `manual_papers.json`; la *ingestión*
asigna `paper_id`/`global_id`, evita duplicados por `global_id`, copia el PDF a la carpeta de trabajo
(`pdfs/`) y lo fusiona en el conjunto seleccionado. Si no hay entradas, ambos módulos se omiten.

- **Toma (scanner):** PDFs de `results/input/{subject}/manual_papers/` y, si `--manuscript 1`, los PDFs
  de `manual_papers/manuscript/`; `manual_papers.json` previo.
- **Deja (scanner):** `manual_papers.json` (con `origin` = `manual` o `manuscript` y `bibkey`).
- **Toma (ingestión):** `manual_papers.json` + PDFs referenciados; `selected_papers.json`.
- **Deja (ingestión):** `selected_papers.json` (con `origin`, `bibkey`, `local_pdf_path`
  y `acquisition_status="manual_local"`).

### 6. Adquisición de PDFs
**Módulo:** `sources/adquisitionEngine.py` · `processAcquisitionEngine()`

**Propósito:** Descarga el texto completo (PDF) de cada paper seleccionado resolviendo DOI/URLs,
con intervención manual para recuperaciones fallidas y una "bóveda" de PDFs (`_pdf_vault`) para
reutilizar descargas ya hechas.

- **Toma:** `selected_papers.json`.
- **Deja:** PDFs en `results/output/{subject}/pdfs/`, `selected_acquired.json`, `pdf_download_log.json`
  (índice determinista `paper_id → pdf_file` + eventos), `missing_pdfs.json`, `pdf_inventory.json`
  (índice de la bóveda).
- *Nota:* si el paper ya trae `local_pdf_path` (ingesta manual/manuscrito), no se descarga: se
  acepta el PDF local y se marca `acquisition_status="manual_local"`.

### 7. Limpieza de corpus
**Módulo:** `sources/corpusCleaning.py` · `processCorpusCleaning()`

**Propósito:** Extrae el texto de los PDFs (vía el servicio federado `fullTextExtractionEngine`,
que excluye el contenido de tablas), aplica filtros de calidad (mínimo de palabras/caracteres) y una
limpieza regex (URLs, emails, corte en bibliografía, números de página, etc.). Para el corte de la
bibliografía usa el servicio compartido `referenceFilter` (`strip_reference_tail`). Produce los
contratos de texto y metadatos que alimentan toda la fase de análisis.

- **Toma:** `selected_acquired.json` + `pdf_download_log.json` (índice `paper_id → pdf_file`) y los
  PDFs en disco.
- **Deja:** `papers_metadata.json` (metadatos + estadísticas post-limpieza) y `papers_text.json`
  (texto limpio + secciones).
- *Nota:* la asociación paper↔PDF se resuelve de forma determinista desde el índice
  `pdf_download_log.json` (módulo `sources/common/pdfIndex.py`); `local_pdf_path` queda como
  respaldo para ejecuciones antiguas. No se reconstruye a partir del nombre del fichero ni del título.

### 8. Descubrimiento
**Módulo:** `sources/discoveryEngine.py` · `processDiscoveryEngine()`

**Propósito:** Descubrimiento guiado por las preguntas de investigación: identifica qué papers del
corpus son relevantes para cada RQ (concepto) y produce candidatos concepto→paper.

- Al aplanar los clusters de todas las RQs, fusiona conceptos con etiquetas casi idénticas
  (similitud coseno ≥ `discovery.concept_dedup_threshold`, por defecto 0.88) y remapea
  `doc_topic_map` a los representantes.
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
alineado, configurable desde `studyDescription.json`. Antes de segmentar, descarta la cola de
bibliografía (`referenceFilter.strip_reference_tail`) para no generar evidencias sobre referencias.

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

- Descarta ventanas que son bibliografía (`looks_like_reference`) y deduplica evidencias por
  `doc_id` antes de sintetizar.
- **Atribución a nivel de afirmación:** el prompt numera las evidencias `[1..n]` y pide citarlas
  inline; cada hallazgo persiste el mapa `[n] → doc_id` en `concept_findings.json > findings[].citations`.
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
y del corpus, con traducción configurable (caché en `translation_cache.json`; backend local
`Helsinki-NLP/opus-mt-en-es` con respaldo Google). Filtra fragmentos que son bibliografía
(`referenceFilter.looks_like_reference`).

- **Citas inline:** los hallazgos se renderizan con marcadores `[n]` remapeados a una numeración
  global por orden de aparición, con una sección "Referencias citadas (atribución por afirmación)".
  La traducción preserva dichos marcadores.

- **Flags:** con `--emergente 0` se omiten los conceptos emergentes (sin llamadas al LLM, pero se
  conservan sus papers para PRISMA y bibliografía); con `--manuscript 1` se añade la sección
  "Evidencias en el manuscrito".
- **Toma:** `concept_findings.json` + `candidate_concepts.json` + `aligned_concepts.json` +
  `technical_annex.json` + `papers_text.json` + `papers_metadata.json` + `conceptsQuery.json` +
  `studyDescription.json` (+ `manuscript_refs.json` si `--manuscript 1`).
- **Deja:** `training_materials.md` (+ `manuscript_context_cache.json` si `--manuscript 1`).

### 17. Trabajo relacionado
**Módulo:** `sources/relatedWork.py` · `processRelatedWork()`

**Propósito:** Genera un documento de *related work* (trabajo relacionado) sintetizando los hallazgos
por pregunta de investigación, con los DOI/metadatos de los papers involucrados.

- **Toma:** `concept_findings.json` + `papers_metadata.json` (DOIs).
- **Deja:** `related_work.md`.

### Módulos de soporte (no son pasos del pipeline)

#### A. Evidencias del manuscrito
**Módulo:** `sources/manuscriptEvidence.py`

**Propósito:** Concentra toda la lógica del manuscrito del investigador (se activa con
`--manuscript 1`). Responsabilidades:

1. **Parseo** de la bibliografía BibTeX (`biblio.bib`) y de los **contextos de cita** del `.tex`
   (el texto que menciona cada `\cite{key}`).
2. **Asociación PDF ↔ bibkey**: determinista por nombre de fichero, con respaldo por bóveda
   `_pdf_vault`, DOI (leído del propio PDF) y título. Los no asociados se reportan.
3. **Contrato `manuscript_refs.json`**: por referencia incluye `title`, `doi`, `cited`, `in_bib`,
   `has_pdf`, `status` (`pdf`/`cited_no_pdf`/`bib_only`), `pdf_file` y `contexts`; más los agregados
   `unmatched_pdfs` y `cited_without_bib`.
4. **Construcción de la evidencia** (`ManuscriptEvidenceBuilder`): resuelve cada referencia contra
   el corpus por DOI/título, extrae sus **datos estructurados** (reutilizando
   `enrich_evidences.PaperSectionExtractor`) y genera una **pequeña extensión de la cita** (1-2
   frases, en español) con *grounding* en abstract + datos estructurados. Usa LLM con **caché**
   (`manuscript_context_cache.json`) y **respaldo extractivo** (abstract) si no hay LLM.
5. **Señalización de faltantes**: las referencias citadas sin PDF se marcan (⚠️), indicando si la
   bibkey no existe en el `.bib`; se listan también los PDFs sin referencia asociada.

- **Toma:** `manual_papers/manuscript/{manuscript.pdf, *.tex, *.bib, <bibkey>.pdf}` +
  `papers_text.json` + `papers_metadata.json` (corpus existente).
- **Deja:** `manuscript_refs.json` (+ `manuscript_context_cache.json`).
- **Consumido por:** `manualPapersScanner` (para añadir los PDFs al circuito) y `trainingMaterials`
  (para la sección "Evidencias en el manuscrito").

#### B. Filtro de referencias
**Módulo:** `sources/common/referenceFilter.py`

**Propósito:** Servicio compartido para detectar y eliminar bibliografía:

- `strip_reference_tail(text)`: recorta la cola de referencias de un texto/sección.
- `looks_like_reference(text)`: heurística que decide si un fragmento es una lista de referencias
  (no evidencia real).

- **Usado por:** `corpusCleaning`, `conceptEvidence` y `trainingMaterials`.

---

## 📝 4. Notas

- **Ingesta API-first:** PubMed/Scopus/IEEE pueden consultarse vía API (`search.providers.<fuente>.mode = "api"`).
  Las respuestas crudas se congelan en `results/input/{subject}/_raw/` para re-ejecutar la
  normalización sin red (reproducibilidad). `search.refresh = true` fuerza reconsulta. Las
  credenciales viven en `config.json` (fichero ignorado por git).
- **Extracción de texto:** el módulo `fullTextExtractionEngine.py` es un *servicio* reutilizable
  (no un paso del pipeline) usado por `corpusCleaning`. El `processFullTextExtraction` que aparece
  comentado en `main.py` está deprecado: la extracción se integra en el paso 7.
- **Puntos de interacción manual:** paso 4 (selección interactiva), paso 5 (ingestión manual de
  PDFs) y paso 6 (recuperación manual de PDFs fallidos).
- **Fuentes de búsqueda:** la lista activa se controla con `config.json > search.availables`
  (`wos`, `scopus`, `ieee`, `pubmed`).
- **Manuscrito:** requiere `manual_papers/manuscript/manuscript.pdf`; los PDFs asociados se ingieren
  como `manual_papers` con `origin="manuscript"`. El propio `manuscript.pdf` nunca se ingesta.
  Las citas que ya están en el corpus se enriquecen sin necesidad de PDF.
- **Salidas del manuscrito (si `--manuscript 1`):** `manuscript_refs.json`,
  `manuscript_context_cache.json` y la sección "Evidencias en el manuscrito" en
  `training_materials.md`.
- **Traducción:** backend local `Helsinki-NLP/opus-mt-en-es` (requiere `transformers` +
  `sentencepiece`/`sacremoses`) con respaldo `deep_translator` (Google); caché en
  `translation_cache.json`.