# Scientific Literature Intelligence Pipeline (SLIP)

## Overview

This document describes the architecture of a local AI-assisted scientific literature analysis system designed for doctoral research. The objective is to automate the most repetitive stages of literature review while preserving the researcher's control over relevance assessment and scientific interpretation.

The system is intended to support the following workflow:

1. Retrieve candidate publications from scientific databases.
2. Download available PDFs automatically.
3. Extract and structure paper content.
4. Generate research-subject-oriented summaries.
5. Assist relevance assessment.
6. Extract predefined conceptual dimensions from selected papers.
7. Build a cumulative evidence matrix.
8. Create a searchable scientific knowledge base.

The architecture prioritizes:

* Reproducibility.
* Traceability.
* Local execution.
* Explainability.
* Human-in-the-loop validation.

---
## Módulo de Alineamiento de Conceptos (concept_alignment.py)
Asigna documentos completos a conceptos de investigación. Responde: "¿Qué papers son relevantes para cada pregunta de investigación?"
Entradas
Elemento	Formato	Procedencia	Descripción
Conceptos candidatos (descubiertos)	concept_candidates.json (JSON)	Módulo de descubrimiento	Tópicos emergentes identificados automáticamente en la literatura, con sus documentos asociados, keywords y topic_label.
Conceptos objetivo	conceptsQuery.json (JSON)	Definición del investigador	Preguntas de investigación definidas en el proyecto, con sus id, query, name, description y expansion_prompt.
Corpus de papers	papers_text.json (JSON)	Módulo de extracción	Texto completo de los papers con sus secciones estructuradas.
Focus terms	studyDescription.json (JSON)	Configuración del proyecto	Términos prioritarios que reciben mayor peso en el alineamiento y re-ranking.
Salidas
Elemento	Formato	Descripción
Conceptos alineados	aligned_concepts.json (JSON)	Estructura donde cada concepto objetivo contiene la lista de documentos más relevantes asignados, con sus puntuaciones de alineamiento y re-ranking.
Alineamientos	aligned_concepts.json (JSON)	Registro de la asignación de cada tópico descubierto a conceptos objetivo, con sus puntuaciones de similitud.
Estructura de salida (por concepto alineado)
json
{
  "concept_id": 0,
  "concept_name": "Arquitecturas de la atención",
  "concept_query": "How do recommendation systems structurally exploit human attention...",
  "concept_description": "Analysis of the technical architecture...",
  "concept_type": "evaluation",
  "n_documents": 15,
  "documents": [
    {
      "doc_id": "wos_5",
      "title": "Toward explainable affective computing: A review",
      "text": "...",
      "section_text": "..."
    }
  ],
  "document_scores": [0.78, 0.72, 0.68, ...]
}
Proceso
1. Preparación y carga
Lee el archivo concept_candidates.json con los conceptos descubiertos automáticamente.

Lee el archivo conceptsQuery.json con los conceptos objetivo definidos por el investigador.

Carga el corpus de papers con sus títulos y secciones estructuradas.

Carga los focus_terms desde el archivo de configuración del proyecto.

2. Generación de textos para embeddings
Para conceptos descubiertos (tópicos emergentes):

Construye un texto representativo combinando:

topic_label

keywords (hasta 5)

Fragmentos de documentos relevantes (hasta 10 documentos, máximo 4000 caracteres por documento)

Para conceptos objetivo (preguntas de investigación):

Construye un texto combinando:

name

query

description

expansion_prompt (expandido opcionalmente con LLM)

focus_terms (si existen)

3. Generación de embeddings
Usa el modelo BAAI/bge-large-en-v1.5 para generar embeddings de todos los textos.

Los embeddings se generan en batch para eficiencia.

Modo mejorado (USE_ENHANCED_EMBEDDINGS): Para los conceptos descubiertos, el texto representativo se enriquece con secciones específicas de los documentos (Introduction, Methods, Results, Conclusion) con ponderaciones diferenciadas (ver section_indexer.py).

4. Cálculo de alineamientos
Calcula la matriz de similitud coseno entre todos los conceptos descubiertos y todos los conceptos objetivo.

Para cada concepto descubierto, selecciona los top-k conceptos objetivo más similares (por defecto, TOP_K_ASSIGNMENTS = 3).

Filtra las asignaciones que no superan el umbral de ALIGNMENT_SCORE_THRESHOLD = 0.40.

5. Agregación de documentos por concepto objetivo
Para cada concepto objetivo, agrupa todos los documentos provenientes de los tópicos descubiertos que se le han asignado.

Cada documento mantiene su puntuación de alineamiento (alignment_score).

6. Re-ranking dinámico por concepto
Para cada concepto objetivo, toma los documentos agregados y los re-ordena usando un scoring específico para ese concepto.

El re-ranking calcula la similitud entre:

El texto representativo del concepto objetivo (enriquecido con focus terms y expansion_prompt).

El texto de cada documento (título + sección relevante).

Aplica un filtro por percentil: retiene solo los documentos que superan el percentil configurado (por defecto, 60% superior).

Garantía mínima: si el filtrado elimina todos los documentos, se conservan al menos MIN_DOCS_AFTER_RERANK = 5.

Score final combinado: 0.3 * alignment_score + 0.7 * rerank_score (configurable).

7. Ensamblaje de la salida
Para cada concepto objetivo, selecciona los TOP_DOCS_PER_CONCEPT (por defecto 30) documentos mejor puntuados.

Construye la estructura final con los documentos y sus puntuaciones.

8. Reporte y persistencia
Guarda el resultado en aligned_concepts.json.

Genera un resumen estadístico en el log con el número de documentos por concepto.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            ENTRADA                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  concept_candidates.json    │  conceptsQuery.json    │  papers_text.json   │
│  (tópicos descubiertos)     │  (conceptos objetivo)  │  (corpus completo)  │
└──────────────────────┬──────────────────┬─────────────────┬─────────────────┘
                       │                  │                 │
                       ▼                  ▼                 ▼
              ┌─────────────────────────────────────────────────┐
              │          PROCESO PRINCIPAL                       │
              ├─────────────────────────────────────────────────┤
              │  1. Construir textos para embeddings           │
              │     - Conceptos descubiertos: topic_label +    │
              │       keywords + fragmentos de papers          │
              │     - Conceptos objetivo: query + description  │
              │       + expansion_prompt + focus_terms         │
              │  2. Generar embeddings (batch)                 │
              │  3. Calcular similitud coseno (matriz)         │
              │  4. Top-K asignaciones por tópico              │
              │  5. Agregar documentos por concepto objetivo   │
              │  6. Re-ranking dinámico por concepto:          │
              │     - Similitud documento-concepto             │
              │     - Filtro por percentil                    │
              │     - Garantía mínima de documentos            │
              │     - Score combinado: alignment + rerank      │
              │  7. Seleccionar top-N documentos por concepto  │
              └─────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            SALIDA                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  aligned_concepts.json                                                     │
│  (conceptos objetivo → documentos asignados con puntuaciones)             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Módulo de Extracción de Evidencias (concept_evidence.py)
Extrae fragmentos de texto (evidencias) de esos documentos detectados por concept_alignment. Responde: "¿Qué partes concretas de cada paper son relevantes para la pregunta?"
Encargado de extraer fragmentos de texto relevantes (evidencias) de los papers académicos para cada concepto de investigación definido en el proyecto. Su función es transformar documentos completos (papers) en unidades de conocimiento (knowledge_units): fragmentos de texto que son semánticamente relevantes para las preguntas de investigación de cada concepto.

En esencia, este módulo resuelve el problema de cómo pasar de un paper completo a un conjunto de fragmentos significativos que respondan a una pregunta de investigación concreta, aplicando técnicas de scoring híbrido (embedding semántico + BM25) y filtrado por relevancia.

### Entradas del Módulo `concept_evidence.py`

| Elemento | Formato | Procedencia | Descripción |
|----------|---------|-------------|-------------|
| **Conceptos alineados** | `aligned_concepts.json` (JSON) | Módulo anterior (`concept_alignment.py`) | Estructura con conceptos de investigación, sus queries, y los documentos (papers) asociados. |
| **Secciones de los papers** | `papers_text.json` (JSON) | Módulo anterior (`corpusCleaning.py`) | Secciones (`clean_sections`) de cada paper; permiten extraer evidencias de **todas** las secciones (no solo la alineada), ponderadas por `section_weights`. |
| **Focus terms** | `studyDescription.json` (JSON) | Configuración del proyecto | Lista de términos prioritarios que reciben mayor peso en el scoring (ej. "HRV", "dark patterns", "XAI"). |
| **Configuración** | `studyDescription.json` (`project.evidence_extraction`) y `config.json` (`defaults.conceptEvidence`) | Configuración del proyecto | Modelo de embeddings, tamaño de ventana, pesos de scoring, umbrales de filtrado, `section_weights`, `genericity_penalty`, `genericity_topk`, `generic_threshold`, `consensus_enabled`, `consensus_cluster_threshold`. |
### Salidas del Módulo `concept_evidence.py`

| Elemento | Formato | Descripción |
|----------|---------|-------------|
| **Evidencias diferenciales por concepto** | `concept_evidence.json` (JSON) | Cada concepto contiene una lista de `knowledge_units` (fragmentos diferenciales). Cada unidad incluye `doc_id`, título, `similarity`, `section` y `genericity`. |
| **Consenso general** | `concept_evidence.json` (JSON) | Cada concepto incluye `consensus`: fragmentos genéricos (repetidos entre papers) agrupados, con `representative_text`, `doc_ids`, `n_papers` y `genericity`. |
| **Expansión de queries** | Interno | Queries enriquecidas con términos adicionales generados por un LLM para mejorar la recuperación de fragmentos relevantes. |

---

**Estructura de salida (por concepto):**

```json
{
  "concept_id": 0,
  "concept_query": "How do recommendation systems structurally exploit human attention...",
  "knowledge_units": [
    {
      "doc_id": "wos_5",
      "paper_title": "Toward explainable affective computing: A review",
      "evidence_text": "The model learns from behavioral signals...",
      "similarity": 0.78,
      "section": "results",
      "genericity": 0.42
    }
  ],
  "consensus": [
    {
      "representative_text": "AI tools can support inclusive education by personalizing learning...",
      "section": "results",
      "doc_ids": ["wos_1", "wos_3", "wos_9"],
      "n_papers": 10,
      "genericity": 0.86
    }
  ]
}
```

**Nota**: El archivo incluye también metadatos del pipeline: `schema_version`, `embedding_model` y `focus_terms_used`.
### Proceso
1. Preparación y carga
Lee el archivo de entrada aligned_concepts.json con los conceptos y sus documentos.

Carga los focus_terms desde el archivo de configuración del proyecto.

2. Expansión de queries (opcional)
Para cada concepto, toma la query original y la expande usando un LLM (por defecto qwen3:8b).

Genera hasta 7 términos adicionales relacionados semánticamente con la pregunta de investigación.

Esto mejora la cobertura de la búsqueda, capturando fragmentos que usan vocabulario diferente al de la query original.

3. Preparación del documento
Segmentación en oraciones: Usa spaCy para dividir el texto del paper en oraciones.

Creación de ventanas deslizantes: Combina oraciones en ventanas de tamaño configurable (por defecto 5 oraciones, con paso de 2). Esto permite capturar fragmentos con contexto, no solo oraciones aisladas.

Filtro de ruido: Elimina ventanas que son demasiado cortas o contienen patrones de ruido (ej. referencias bibliográficas, texto de copyright, urls).

4. Scoring de ventanas
Para cada ventana, se calcula un score híbrido que combina:

Embedding semántico (70% peso): Similitud coseno entre el embedding de la ventana y el embedding de la query del concepto. Calculado en batch para eficiencia.

BM25 (30% peso): Relevancia basada en coincidencia de términos clave.

Bonificación por términos de investigación (claim_boost): +0.05 por cada término de la lista de "claim patterns" (result, find, show, demonstrate, improve, etc.).

Bonificación por focus terms: +0.15 por cada focus term presente en la ventana.

Principio clave de rendimiento: Todos los embeddings se calculan en batch — una sola llamada al modelo por conjunto de ventanas, no ventana por ventana.

5. Filtrado y deduplicación
a) Deduplicación por texto exacto: Elimina fragmentos duplicados dentro de un mismo documento.

b) Clusterización intra-documento (NOVEDAD): Si un documento tiene múltiples evidencias, se agrupan usando clustering jerárquico (umbral de distancia 0.40). De cada cluster, se retiene solo la evidencia con mayor similitud, reduciendo la redundancia semántica.

c) Filtrado por percentil: Retiene solo las evidencias que superan el percentil configurado (por defecto, el 70% superior en similitud).

d) Piso absoluto de similitud: Descartar evidencias con similitud < 0.30.

e) Rescate de documentos sin representación: Si algún documento no tiene evidencias que hayan pasado los filtros, se rescata su mejor evidencia si supera el umbral mínimo de 0.50.

f) Límite máximo: Máximo de 80 evidencias por concepto.

6. Ponderación por sección, genericidad y consenso
Ponderación por sección (`section_weights`): las ventanas se generan sobre todas las secciones del paper y se multiplican por el peso de su sección (results 1.0, conclusion 0.9, methodology 0.8, introduction 0.5, abstract 0.4). Así se favorece lo diferencial frente al enunciado genérico del problema.

Genericidad: para cada ventana se calcula su similitud media (top-k) a las ventanas de otros papers. Alta genericidad = texto que se repite en el corpus.

Separación consenso/diferencial: las ventanas con `genericity ≥ generic_threshold` se agrupan en `consensus` (bloque "Consenso general (ya conocido)", con representantes y `n_papers`); el resto quedan como evidencias diferenciales. La genericidad no modifica el score del filtro (solo clasifica/ordena), de modo que no se promocionan rarezas irrelevantes.

7. Persistencia y reporte
Guarda el resultado en concept_evidence.json (con `knowledge_units` y `consensus` por concepto).

Genera un resumen estadístico en el log con el número de unidades de conocimiento por concepto y documentos representados.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            ENTRADA                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  aligned_concepts.json          │  studyDescription.json                    │
│  (conceptos + documentos)       │  (focus_terms + configuración)           │
└──────────────────────┬─────────────────────┬───────────────────────────────┘
                       │                     │
                       ▼                     ▼
              ┌─────────────────────────────────────────────────┐
              │          PROCESO PRINCIPAL                       │
              ├─────────────────────────────────────────────────┤
              │  1. Expansión de queries (LLM)                  │
              │  2. Para cada concepto:                         │
              │      Para cada documento:                       │
              │        3. Dividir en oraciones                  │
              │        4. Ventanas deslizantes                  │
              │        5. Filtro de ruido                       │
              │        6. Scoring híbrido (embedding + BM25)    │
              │        7. Deduplicación por texto               │
              │        8. Clusterización intra-documento        │
              │  9. Filtrado global (percentil + piso + rescate)│
              │  10. Límite de 80 evidencias por concepto       │
              └─────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            SALIDA                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  concept_evidence.json                                                    │
│  (conceptos → knowledge_units diferenciales + consensus)                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Módulo de Clustering de Evidencias (clusterEvidences.py)

Encargado de agrupar semánticamente los fragmentos de texto (evidencias) extraídos de los papers académicos en función de su similitud de contenido, dentro de cada concepto de investigación. Su objetivo es identificar grupos naturales de evidencias relacionadas (clusters) que compartan temática, lo que permitirá que el módulo de síntesis posterior genere hallazgos más coherentes y robustos.

### Entradas
Elemento	Formato	Procedencia	Descripción
Evidencias por concepto	concept_evidence.json (JSON)	Módulo anterior	Estructura con `knowledge_units` diferenciales (fragmentos con `doc_id`, `paper_title`, `similarity`, `section` y `genericity`) y el bloque `consensus` (fragmentos genéricos repetidos entre papers).
Focus terms	studyDescription.json (JSON)	Configuración del proyecto	Lista de términos prioritarios para el proyecto (ej. "HRV", "dark patterns", "XAI"). Se usan para relajar el umbral de clustering en conceptos con alta densidad de focus terms, permitiendo clusters más grandes y generales.
### Salidas
Elemento	Formato	Descripción
Evidencias clusterizadas	clustered_evidences.json (JSON)	Estructura donde cada concepto contiene una lista de clusters, y cada cluster incluye: tamaño, score de similitud promedio, lista de evidencias, identificadores de papers, título sugerido del cluster (basado en la evidencia más representativa), y métricas de enriquecimiento con focus terms.
Estructura de salida (por concepto)
```json
{
  "concept_id": 0,
  "concept_query": "How do recommendation systems...",
  "focus_terms_used": ["HRV", "EDA", "dark patterns"],
  "focus_density": 0.45,
  "total_evidences": 27,
  "n_clusters": 4,
  "consensus": [
    {"representative_text": "AI tools can support inclusive...", "section": "results",
     "doc_ids": ["wos_1", "wos_3"], "n_papers": 10, "genericity": 0.86}
  ],
  "clusters": [
    {
      "cluster_id": 0,
      "size": 8,
      "avg_evidence_score": 0.72,
      "focus_rich_count": 5,
      "focus_rich_percentage": 62.5,
      "suggested_theme": "El contenido de alto arousal...",
      "papers": ["wos_5", "ieee_75"],
      "paper_titles": ["Toward explainable affective..."],
      "evidences": [
        {"text": "...", "doc_id": "wos_5", "similarity": 0.85}
      ]
    }
  ]
}
```
### Proceso
1. Preparación y carga
Lee el archivo de entrada concept_evidence.json con los fragmentos ya asignados a conceptos.

Carga los focus_terms desde el archivo de configuración del proyecto.

2. Extracción de textos y metadatos
Por cada concepto, extrae los textos de las evidencias y sus metadatos (doc_id, título, similitud).

3. Cálculo de focus_density por concepto
Calcula la fracción de evidencias que contienen al menos un focus term dentro del conjunto de evidencias del concepto.

Este valor se usa para decidir el umbral de clustering (ver paso 5).

4. Generación de embeddings
Usa el modelo BAAI/bge-base-en-v1.5 de SentenceTransformer para convertir cada texto en un vector de alta dimensionalidad (768 dims).

Normaliza los vectores para que la similitud coseno sea eficiente.

5. Clustering jerárquico aglomerativo
Aplica AgglomerativeClustering con métrica coseno y linkage average.

El umbral de distancia se determina dinámicamente según:

Número de evidencias del concepto (cuantos más, más fino el umbral → clusters más pequeños y específicos).

Focus density: si ≥ 30% (umbral configurable), se relaja el umbral en un 5%, produciendo clusters más grandes y generales, lo que favorece que evidencias con términos prioritarios se agrupen juntas.

Esto produce una asignación de cada evidencia a un número variable de clusters (puede ser 1 si todas son muy similares, o muchos si son heterogéneas).

6. Generación de resumen por cluster
Para cada cluster, calcula:

Tamaño, puntuación promedio y máxima de similitud.

Porcentaje de evidencias que contienen focus terms (para trazabilidad).

Tema sugerido: toma la evidencia con mayor "peso" (score + bonificación por focus terms) y usa sus primeras 150 caracteres como tema tentativo.

Lista de papers únicos que contribuyen a ese cluster.

7. Reporte y persistencia
Genera un informe detallado en el log para depuración.

Guarda el resultado final en clustered_evidences.json.

8. Resumen estadístico final
Registra: total de evidencias procesadas, número de clusters formados, asignaciones paper-concepto (un paper puede estar en varios conceptos), y papers únicos totales.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            ENTRADA                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  concept_evidence.json          │  studyDescription.json                    │
│  (evidencias por concepto)      │  (focus terms)                           │
└──────────────────────┬─────────────────────┬───────────────────────────────┘
                       │                     │
                       ▼                     ▼
              ┌─────────────────────────────────────────────────┐
              │          PROCESO PRINCIPAL                      │
              ├─────────────────────────────────────────────────┤
              │  1. Extraer textos y metadatos                  │
              │  2. Calcular focus_density (fracción de textos  │
              │     con focus terms)                            │
              │  3. Generar embeddings con bge-base-en-v1.5     │
              │  4. Clusterizar (AgglomerativeClustering)       │
              │     con umbral dinámico según:                  │
              │     - Número de evidencias                      │
              │     - focus_density (relaja umbral si ≥ 30%)    │
              │  5. Generar resumen de cada cluster:            │
              │     - Tamaño, scores, papers                    │
              │     - Tema sugerido (mejor evidencia)           │
              │     - % de evidencias con focus terms           │
              └─────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            SALIDA                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  clustered_evidences.json                                                  │
│  (clusters con sus evidencias, metadatos y métricas)                      │
└─────────────────────────────────────────────────────────────────────────────┘
```


## Módulo de Síntesis de Hallazgos (synthesize_findings.py)

Transformar evidencias académicas clusterizadas en hallazgos sintetizados, traducidos y filtrados por relevancia semántica.


### Entradas del Módulo `synthesize_findings.py`

| Elemento | Formato | Procedencia | Descripción |
|----------|---------|-------------|-------------|
| **Evidencias clusterizadas** | `clustered_evidences.json` (JSON) | Módulo anterior (`clusterEvidences.py`) | Agrupaciones de fragmentos de papers organizadas por concepto de investigación, con métricas de similitud semántica. |
| **Textos completos de papers** | `papers_text.json` (JSON) | Módulo de extracción | Estructura de texto completo de cada paper, con secciones (abstract, introduction, methods, results, conclusion). |
| **Focus terms** | `studyDescription.json` (JSON) | Configuración del proyecto | Términos prioritarios que deben recibir mayor peso en la síntesis (ej. "HRV", "dark patterns", "XAI"). |
| **Configuración** | `studyDescription.json` (JSON) | Configuración del proyecto | Parámetros: modelo LLM, modelo de embeddings, umbrales de relevancia, estilo de hallazgo, etc. |

### Salidas del Módulo `synthesize_findings.py`

| Elemento | Formato | Descripción |
|----------|---------|-------------|
| **Hallazgos por concepto** | `concept_findings.json` (JSON) | Para cada concepto: lista de hallazgos sintetizados, cada uno con estructura semántica (contribución, metodología, resultados, implicaciones), papers soporte, evidencias originales y métricas de calidad. |
| **Caché de traducciones** | `translation_cache.json` (JSON) | Persistencia de traducciones para evitar re-procesar textos ya traducidos en ejecuciones futuras. |

---

**Estructura de salida (por hallazgo):**

```json
{
  "finding": "Texto plano del hallazgo (sin headers)",
  "finding_structured": {
    "contribucion": "Contribución principal del hallazgo",
    "metodologia": "Metodología y enfoque",
    "resultados": "Resultados clave",
    "implicaciones": "Implicaciones y limitaciones"
  },
  "finding_markdown": "Texto formateado con headers para renderizado",
  "supporting_papers": ["paper_id_1", "paper_id_2"],
  "evidence_count": 5,
  "avg_evidence_score": 0.78,
  "top_quotes": [...]
}
```

**Nota**: El `concept_findings.json` es la entrada principal para el generador del documento `training_materials.md`, donde los hallazgos se integran en el flujo de MarkdownRenderer. La caché de traducciones (`translation_cache.json`) evita la regeneración de traducciones ya realizadas en ejecuciones anteriores, ahorrando tiempo y coste computacional.
### Proceso
El módulo opera en dos pasadas explícitas y trazables, separando el costo de inferencia LLM del costo de embedding.

Pasada 1: Generación de Hallazgos (intensiva en LLM)
Carga y validación: Verifica que existan los archivos de entrada (clustered_evidences.json, papers_text.json).

Extracción y traducción de evidencias: Para cada evidencia, extrae las secciones relevantes del paper (introducción, métodos, resultados, conclusiones) y las traduce al español usando el LLM configurado (por defecto qwen3:8b), con caché en disco para evitar retraducciones.

Generación de hallazgos por cluster: Para cada cluster de evidencias (grupo de fragmentos relacionados dentro de un concepto):

Selecciona las evidencias de mayor calidad (por similitud).

Construye un prompt específico según el estilo configurado (structured, narrative o brief).

Invoca al LLM para generar un hallazgo sintetizado en español.

Parsea la respuesta en secciones estructuradas (contribución, metodología, resultados, implicaciones).

Pasada 2: Filtrado por Relevancia Semántica (intensiva en embeddings)
Cálculo de similitud semántica:

Genera el embedding de la query del concepto (una sola vez).

Genera los embeddings de todos los hallazgos en un único batch.

Calcula la similitud coseno entre cada hallazgo y la query del concepto.

Filtrado por percentil: Conserva únicamente los hallazgos que superan el percentil configurado (por defecto, el 60% superior).

Garantía mínima: Si el filtrado elimina todos los hallazgos, se conserva el mejor.

Boost por focus terms: Incrementa la puntuación de hallazgos que mencionan términos prioritarios del proyecto.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            ENTRADA                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  clustered_evidences.json   │  papers_text.json   │  studyDescription.json  │
│  (evidencias agrupadas)     │  (texto completo)   │  (configuración)        │
└──────────────────────┬─────────────────┬─────────────────┬─────────────────┘
                       │                 │                 │
                       ▼                 ▼                 ▼
              ┌─────────────────────────────────────────────────┐
              │               PASADA 1: GENERACIÓN             │
              ├─────────────────────────────────────────────────┤
              │  1. Extracción de secciones del paper           │
              │  2. Traducción al español (con caché)           │
              │  3. Construcción de prompt por cluster          │
              │  4. LLM → hallazgo estructurado                │
              │  5. Parsing → FindingSections                  │
              └─────────────────────────────────────────────────┘
                       │
                       ▼
              ┌─────────────────────────────────────────────────┐
              │           PASADA 2: FILTRADO SEMÁNTICO          │
              ├─────────────────────────────────────────────────┤
              │  1. Embedding de query del concepto             │
              │  2. Embedding batch de hallazgos               │
              │  3. Similitud coseno (nube de puntos)          │
              │  4. Filtrado por percentil                     │
              │  5. Boost por focus terms                      │
              └─────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            SALIDA                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  concept_findings.json  │  translation_cache.json                         │
│  (hallazgos filtrados   │  (persistencia de traducciones)                 │
│   y estructurados)      │                                                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

