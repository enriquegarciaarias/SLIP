# SLIP Pipeline - Estado de Sesión
**Fecha:** 2026-10-16
**Última actualización:** Pipeline completado exitosamente

---

## ✅ ESTADO ACTUAL: PIPELINE FUNCIONAL END-TO-END

### Última ejecución exitosa
```
main.py --proc SLIP → COMPLETED
- training_materials.md ✅
- related_work.md ✅
- technical_annex.json ✅
```

---

## 📋 RESUMEN DE MEJORAS IMPLEMENTADAS

### 1. Section-Weighted Embeddings (`discoveryEngine`)
- **Archivo:** `sources/discoveryEngine.py`
- **Config:** `config.json` → `section_weights.presets.technical_paper`
- **Pesos:** methodology=2.0, results=2.0, abstract=1.8, title=2.0, conclusion=1.2, intro=0.5, related_work=0.2, rest=0.0
- **Aliases:** methods→methodology, findings→results, background→related_work
- **Resultado:** +6 topics (38→44), +4 docs (50→54), mejores RQs técnicas

### 2. Config Unificada (`config.json`)
- **Nueva sección:** `section_weights.presets` con `technical_paper` y `evidence_scoring`
- **Overrides por módulo:** `section_weights_preset` + `section_weight_overrides`
- **Aliases globales:** `section_aliases` para normalizar nombres de sección

### 3. Structure Extraction - NUEVO MÓDULO (`structureExtraction.py`)
- **Entrada:** `papers_text.json` (clean_text + clean_sections)
- **Salida:** `papers_structured.json` (71 papers)
- **Campos extraídos por LLM:**
  - `rq_explicit`, `hypotheses`, `contributions`
  - `methodology.{approach, architecture, datasets, baselines, metrics, signal_modalities}`
  - `results.{main, ablation, key_numbers, tables_referenced}`
  - `limitations`, `future_work`, `key_figures`, `takeaway`
- **Integración:** Se ejecuta tras `corpusCleaning` y antes de `discoveryEngine`

### 4. Concept Mining Engine - MEJORADO (`conceptMiningEngine.py`)
- **Inyección directa** de términos estructurados en KeyBERT:
  - `direct_inject_terms`: key_numbers, metrics, datasets, signal_modalities, baselines, tables
  - **Prioridad:** inyección directa > candidates (focus + structured) > free terms
- **Enriquecimiento de texto:** `build_enriched_text()` incluye secciones virtuales `CONTRIBUTIONS`, `METHODOLOGY_STRUCTURED`, `RESULTS_STRUCTURED`, `TAKEAWAY`, `ARGUMENTATIVE_ZONES`
- **Zoned papers:** Integración de `papers_zoned.json` para priorizar zones CLAIM/EVIDENCE/METHOD
- **Resultado:** **36.7% keywords estructurados** (vs 1-2% antes), 345 métricas, 101 sensores, 45 datasets

### 5. Argumentative Zoning - NUEVO MÓDULO (`argumentativeZoning.py`)
- **Clasificación retórica** de párrafos en 7 zonas:
  - `CLAIM` (contribución/hallazgo), `EVIDENCE` (datos/métricas), `METHOD` (metodología)
  - `BACKGROUND` (contexto), `GAP` (problema), `FUTURE_WORK`, `REST`
- **Clasificador heurístico** basado en patrones lingüísticos + sección
- **Salida:** `papers_zoned.json` (2,788 párrafos clasificados)
- **Distribución:** method=1085, evidence=642, claim=387, background=212, gap=48, rest=12

### 6. Pipeline Principal Actualizado (`main.py`)
```python
processCorpusCleaning()
processArgumentativeZoning()      # NUEVO
processStructureExtraction()      # NUEVO
processDiscoveryEngine()          # Con section-weighted + structured seeds
processConceptMiningEngine()      # Con inyección estructurada + zoning
processConceptAlignment()
processConceptEvidence()
processClusterEvidences()
processEnrichEvidences()
processSynthesizeFindings()
processTechnicalAnnex()
processTrainingMaterials()
processRelatedWork()
```

---

## 📊 RESULTADOS CLAVE

| Métrica | Baseline | Actual | Mejora |
|---------|----------|--------|--------|
| Topics descubiertos | 38 | **44** | +16% |
| Docs cubiertos | 50 | **54** | +8% |
| Keywords estructurados | ~1.8% | **36.7%** | **20x** |
| Keywords métricas | 0 | **345** | ∞ |
| Keywords datasets | 0 | **45** | ∞ |
| Keywords sensores | 101 | **101** | = |
| RQs técnicas (0,1) | 16 topics | **27 topics** | +69% |

### Keywords técnicas reales extraídas:
```
"83.5% classification accuracy", "86.94% accuracy for gait classification",
"Balanced accuracy", "f1-score in generating", "AUC-PR (%)",
"baseline methods", "ecologically valid datasets", "controlled datasets",
"Sentence-level recognition accuracy", "Cohen's Kappa (%)",
"ConMIL", "SSR", "Zero-shot VLM", "Foundation models time series"
```

---

## 📁 ARCHIVOS GENERADOS (Última ejecución)

```
results/output/SensoresACL/
├── candidate_concepts.json              # 44 topics, 54 docs
├── candidate_concepts_by_rq.json        # Desglose por RQ
├── concept_candidates.json              # 5.1MB - keywords con métricas/sensores/datasets
├── concept_evidence.json                # Evidencias por concepto
├── clustered_evidences.json             # Clusters de evidencias
├── enriched_evidences.json              # Evidencias enriquecidas
├── concept_findings.json                # Hallazgos sintetizados
├── technical_annex.json                 # 6 perfiles técnicos (llm_models, signals, metrics, etc.)
├── training_materials.md                # PRISMA + hallazgos + 40 tópicos emergentes
├── related_work.md                      # Related Work listo para paper
├── papers_structured.json               # 71 papers con estructura LLM
├── papers_zoned.json                    # 2,788 párrafos con zona argumentativa
├── papers_text.json                     # Texto limpio + secciones
└── papers_metadata.json                 # Metadatos + word counts
```

---

## ✅ TAREA COMPLETADA: Integración de Argumentative Zoning en conceptEvidence

```python
# conceptEvidence.py → scoring híbrido por zona argumentativa
ZONE_EVIDENCE_WEIGHTS = {
    "claim": 2.0,
    "evidence": 1.8,
    "method": 1.2,
    "gap": 0.8,
    "background": 0.3,
    "rest": 0.0,
}
```
- **Cambios:** `sources/conceptEvidence.py` + `config.json`
- **Mecanismo:** `papers_zoned.json` se carga como lookup `paper_id → {párrafo → zona}`. Cada ventana hereda el peso de zona de su párrafo origen, aplicado como multiplicador sobre el score híbrido (sección × zona).
- **Campo `zone`** añadido al dataclass `Evidence` y serializado en `concept_evidence.json`
- **Resultado (ejecución real):** 142 knowledge units en 6 conceptos; distribución por zona: claim=24, evidence=75, method=43 (background/rest efectivamente filtrados por bajo peso)

### ✅ TAREA COMPLETADA: Extracción de Figure/Table Captions en corpusCleaning

```python
# corpusCleaning.py + fullTextExtractionEngine.py → extraer captions de tablas/figuras
clean_sections["figures_tables_captions"] = "Fig. 1. ... Table 2. ..."
```
- **Cambios:** `sources/common/fullTextExtractionEngine.py`, `sources/corpusCleaning.py`, `config.json`
- **Mecanismo:** Regex detecta captions (`Table N: ...`, `Fig. N: ...`, `Figure N: ...`) en el texto limpio del PDF. Cada caption se asigna a su sección por offset de carácter. Todos los captions se agregan como sección virtual `figures_tables_captions` en `papers_text.json`.
- **Resultado (ejecución real):** 508 captions en 72 PDFs (promedio 7.1 por paper), 68/71 papers con la sección en `papers_text.json`
- **Peso en section_weights:** `figures_tables_captions: 1.8` en ambos presets (`technical_paper` y `evidence_scoring`)
- **Evita ruido:** Solo se extrae el caption (texto descriptivo), no el contenido celular de las tablas (números sueltos, celdas vacías, siglas rotadas)

### ✅ TAREA COMPLETADA: Fine-tuning de Embeddings por Investigación (Auto-detección)

#### Arquitectura multi-investigación — Transparente para el operador

SLIP sirve a múltiples investigaciones (sensores, educación, ciberseguridad, etc.).
Cada investigación tiene su propio directorio bajo `results/input/<investigacion>/` con su
configuración y su modelo de embeddings fine-tuneado.

El operador **siempre ejecuta el mismo comando**:

```bash
python main.py --proc SLIP
```

El pipeline decide automáticamente si hace fine-tuning o no.

#### Directorios

```
results/
  input/
    <investigacion>/
      studyDescription.json
      conceptsQuery.json
      config.json           ← config específico (sobreescribe config.json global)
      models/
        slip-<investigacion>/   ← fine-tuning específico (auto-generado)
          corpus_checksum.txt   ← checksum para detectar cambios en el corpus
  models/
    base/
      slip-scientific-base/     ← fine-tuning general (futuro, punto de partida)
```

#### Config por investigación (`results/input/<investigacion>/config.json`)

```json
{
    "fineTuning": {
        "base_model": "BAAI/bge-base-en-v1.5",
        "output_model_name": "slip-sensores",
        "epochs": 1,
        "batch_size": 16,
        "learning_rate": 2e-5,
        "max_pairs": 2000,
        "pos_neg_ratio": 1.0,
        "min_text_len": 30,
        "max_text_len": 2000,
        "seed": 42
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

#### Merge de configuración (`sources/common/paramsManager.py`)

`manageDefaults()` hace merge profundo entre:
1. `config.json` global (valores por defecto del pipeline)
2. `results/input/<subject>/config.json` (sobrescribe claves específicas)

#### Flujo de ejecución automático (`main.py`)

```
1. corpusCleaning      → papers_text.json
2. argumentativeZoning → papers_zoned.json
3. structureExtraction → papers_structured.json
4. discoveryEngine     → candidate_concepts.json  (usa modelo base en 1ª pasada, fine-tuneado en 2ª+)
5. conceptMiningEngine → concept_candidates.json
6. conceptAlignment    → aligned_concepts.json     ← ahora existe
7. fineTuningEngine    → modelos/slip-<subject>/   ← AUTO-DETECCIÓN
8. conceptEvidence     → concept_evidence.json
...
```

#### Auto-detección de fine-tuning (`sources/fineTuningEngine.py`)

El módulo calcula un checksum SHA-256 de los archivos que definen el corpus:
- `conceptsQuery.json`
- `papers_structured.json`
- `aligned_concepts.json`
- `papers_zoned.json`

| Situación | Acción |
|-----------|--------|
| No existe modelo fine-tuneado | Entrena y guarda el modelo |
| Modelo existe + checksum coincide | Salta entrenamiento (usa el existente) |
| Modelo existe + checksum cambió | Regenera el fine-tuning |

#### Resolución del modelo en `discoveryEngine.py`

- Si `results/input/<subject>/models/slip-<subject>/` existe → usa ese modelo fine-tuneado
- Si NO existe (primera pasada) → usa `BAAI/bge-base-en-v1.5` base sin error

#### Pares de entrenamiento (automáticos, sin etiquetado manual)

| Tipo de par | Fuente | Lógica |
|-------------|--------|--------|
| Positivo CLAIM↔EVIDENCE | `papers_zoned.json` | Párrafos del mismo paper con zonas argumentativas relacionadas |
| Positivo RQ↔Paper | `aligned_concepts.json` + `papers_structured.json` | Query de investigación + contribuciones del paper asignado |
| Positivo Concepto↔Paper | `aligned_concepts.json` + `papers_structured.json` | Concepto descubierto + texto del paper |
| Negativo | Muestreo aleatorio | Textos de papers no relacionados |

Se usa `CosineSimilarityLoss` de sentence-transformers.

#### Cuándo regenerar el fine-tuning específico

| Disparador | ¿Regenerar? |
|------------|-------------|
| Primera ejecución (no existe modelo) | ✅ Sí |
| Corpus cambió (checksum diferente) | ✅ Sí |
| Re-ejecución con mismo corpus | ❌ No (usa el existente) |
| Investigación distinta | ❌ No (cada una tiene su propio modelo) |

#### Nota sobre encoder domain-specific directo (specter2_base)

Se probó `allenai/specter2_base` como reemplazo directo de `bge-base-en-v1.5` y produjo
peores resultados (22 topics vs 44, ruido de afiliaciones). Se mantiene `bge-base-en-v1.5`
como modelo base. El fine-tuning específico por investigación es el camino recomendado
en lugar de cambiar el modelo base.


---

## 🔧 DEUDA TÉCNICA / MEJORAS TÉCNICAS

| Tarea | Esfuerzo | Valor |
|-------|----------|-------|
| **LLM refiner en `argumentativeZoning`** (solo baja confianza) | **Bajo** | **Precisión ↑** | ✅ **COMPLETADO** |
| **Cache LLM persistente cross-runs en `structureExtraction`** | **Medio** | **Velocidad ↑** | ✅ **COMPLETADO** |
| **Proveedor API nativo para arXiv** | **Bajo** | **Ingesta API-first** | ✅ **COMPLETADO** |

---

## 🧪 TESTS UNITARIOS

Archivo: `tests/test_concept_mining_engine.py` (24 tests)

Cubre las funciones destacadas en SESSION_STATE.md como deuda técnica:

| Clase | Funciones probadas | Tests |
|-------|-------------------|-------|
| `TestExtractStructuredTerms` | `extract_structured_terms` | 10 |
| `TestBuildEnrichedText` | `build_enriched_text` | 10 |
| `TestLoadStructuredPapers` | `load_structured_papers` | 3 |
| `TestLoadZonedPapers` | `load_zoned_papers` | 2 |
| `TestDeduplicateSemanticTerms` | `deduplicate_semantic_terms` | 6 |

Casos de prueba incluyen:
- Entrada vacía/None/skipped
- Extracción de contribuciones, metodología, resultados, limitaciones, future_work
- Filtrado de términos cortos (< 3 chars)
- Deduplicación y límite de 80 términos
- Prioridad de términos de alto valor (key_numbers, metrics, datasets, etc.)
- Descomposición de frases largas en n-grams técnicos
- Agregado de secciones virtuales (CONTRIBUTIONS, METHODOLOGY_STRUCTURED, RESULTS_STRUCTURED, TAKEAWAY)
- Agregado de zonas argumentativas (CLAIM, EVIDENCE, METHOD con confianza > 0.3)
- Truncamiento según `max_section_chars`
- Carga de JSONs (listas, archivos faltantes, JSON inválido)

Ejecutar todos los tests:
```bash
cd /home/vant/Documentos/proyectos/LSI/SLIP
.venv/bin/python -m unittest discover tests -v
```

Total: 91 tests (30 nuevos + 61 preexistentes) — todos pasando.

## 🧹 DEDUPLICACIÓN SEMÁNTICA DE KEYWORDS

Archivo modificado: `sources/conceptMiningEngine.py`

Se agregó deduplicación semántica basada en similitud coseno de embeddings para eliminar keywords que son semánticamente muy similares dentro de un mismo tópico.

**Configuración en `config.json` → `conceptMining`:**
```json
"semantic_dedup_enabled": true,
"semantic_dedup_threshold": 0.85
```

**Cómo funciona:**
1. Después de extraer keywords con KeyBERT (prioridad: inyección directa > candidatos focus/structured > libres), se calculan embeddings de todos los términos usando `BAAI/bge-base-en-v1.5`.
2. Se calcula la matriz de similitud coseno entre todos los pares de términos.
3. Si dos términos tienen similitud >= umbral (0.85 por defecto), se elimina el de menor prioridad (el que aparece después en la lista).
4. El modelo de embeddings se carga perezosamente (solo la primera vez que se necesita).

**Archivos modificados:**
- `sources/conceptMiningEngine.py`: Nueva función `deduplicate_semantic_terms()`, `_get_semantic_model()`, integración en `extract_topic_keywords()`, lectura de config `semantic_dedup_threshold` / `semantic_dedup_enabled`.
- `config.json`: Nueva sección `conceptMining.semantic_dedup_threshold` y `semantic_dedup_enabled`.

**Tests:** `TestDeduplicateSemanticTerms` (6 tests) en `tests/test_concept_mining_engine.py`.

## 🧠 LLM REFINER EN ARGUMENTATIVE ZONING

Archivo modificado: `sources/argumentativeZoning.py`

Se activó el clasificador LLM como refiner opcional para párrafos de baja confianza en la clasificación retórica (argumentative zoning).

**Configuración en `config.json` → `argumentativeZoning`:**
```json
"llm_refiner_enabled": false,
"llm_refiner_confidence_threshold": 0.6
```

**Cómo funciona:**
1. Después de la clasificación heurística de cada párrafo, si la confianza es menor al umbral (0.6 por defecto) y el texto tiene más de 100 caracteres, se invoca al LLM para reclasificar.
2. Si el LLM devuelve una confianza mayor que la heurística, se usa la zona del LLM.
3. El LLM se carga perezosamente solo si está habilitado y se necesita.

**Archivos modificados:**
- `sources/argumentativeZoning.py`: Nueva clase `LLMZoner` (descomentada y activada), integración en `processArgumentativeZoning()`, lectura de config `llm_refiner_enabled` / `llm_refiner_confidence_threshold`.
- `config.json`: Nueva sección `argumentativeZoning`.

## 💾 CACHE LLM PERSISTENTE EN STRUCTURE EXTRACTION

Archivo modificado: `sources/structureExtraction.py`

Se agregó un cache persistente de respuestas del LLM para evitar re-llamadas al LLM para el mismo texto entre diferentes runs del pipeline.

**Configuración en `config.json` → `structureExtraction`:**
```json
"llm_cache_enabled": true
```

**Cómo funciona:**
1. Antes de llamar al LLM, se calcula un hash SHA-256 del texto + paper_id como clave de cache.
2. Si existe una entrada en cache para esa clave, se devuelve directamente sin llamar al LLM.
3. Después de cada llamada exitosa al LLM, la respuesta se guarda en cache en disco (`llm_cache.json`).
4. El cache se guarda al final del módulo solo si hubo cambios (`_llm_cache_dirty`).

**Archivos modificados:**
- `sources/structureExtraction.py`: Nuevas funciones `_llm_cache_key()`, `_load_llm_cache()`, `_save_llm_cache()`, `_get_cached_llm_response()`, `_save_cached_llm_response()`, integración en `call_llm_for_extraction()` y `processStructureExtraction()`.
- `config.json`: Nueva sección `structureExtraction.llm_cache_enabled`.

## 🔍 MÚLTIPLES ESTRATEGIAS DE BÚSQUEDA (MÚLTIPLES QUERY)

Archivos modificados: `sources/normalizeSearchResults.py`, `sources/searchMergeEngine.py`, `sources/trainingMaterials.py`, `config.json`, `results/input/<investigacion>/conceptsQuery.json`

SLIP ahora soporta **múltiples estrategias de búsqueda** identificadas por número (01, 02, 03, ...). Cada estrategia tiene su propia query genérica y queries específicas por fuente de datos.

**Configuración en `results/input/<investigacion>/conceptsQuery.json`:**
```json
{
  "query_strategies": {
    "01": {
      "query": "...",
      "queries": {
        "scopus": "...",
        "wos": "...",
        "ieee": "..."
      }
    },
    "02": { ... }
  },
  "concepts": [...]
}
```

**Cómo funciona:**
1. Para cada estrategia definida, `processNormalizeSearchResults` lee archivos de exportación con sufijo de estrategia (ej: `scopus_export_01.csv`, `scopus_export_02.csv`).
2. Cada paper extraído se etiqueta con su `query_strategies` (lista de IDs de estrategias que lo encontraron).
3. `searchMergeEngine` carga todos los archivos `*_search*.json` y propaga `query_strategies` en el merge.
4. `trainingMaterials.py` cuenta papers por estrategia para el PRISMA.

**Archivos de salida:**
- `results/output/<subject>/scopus_search_01.json`
- `results/output/<subject>/scopus_search_02.json`
- etc.

**PRISMA:** Incluye una sección "Por estrategia de búsqueda" mostrando cuántos registros provienen de cada estrategia.

## 💬 CHAT INTELIGENTE (RAG sobre literatura)

Archivo nuevo: `sources/chatEngine.py`

Motor de chat con Retrieval-Augmented Generation (RAG) que permite al investigador hacer preguntas en lenguaje natural sobre los papers y hallazgos del pipeline.

**Cómo funciona:**
1. Carga `papers_text.json`, `concept_evidence.json`, `concept_findings.json` y `papers_structured.json`
2. Genera chunks de texto y sus embeddings con `BAAI/bge-base-en-v1.5`
3. Para cada pregunta, recupera los chunks más relevantes por similitud coseno
4. Usa Ollama (qwen3:8b) para generar la respuesta con citas de fuentes

**Uso:**
```bash
# Como módulo:
from sources.chatEngine import ChatEngine
engine = ChatEngine(output_dir=Path("results/output/SensoresACL"))
engine.indexar()
resultado = engine.preguntar("¿Qué modelos de lenguaje se comparan?")

# Como aplicación web (Streamlit):
streamlit run sources/chatEngine.py -- --ui
```

**Archivos modificados:**
- `sources/chatEngine.py`: Nuevo módulo con clase ChatEngine y interfaz Streamlit

---

## 📝 DOCUMENTACIÓN PENDIENTE ACTUALIZAR

- [x] `README.md` → `docs/README.md` (módulos, flujo, config por investigación)
- [x] `docs/architecture.md` - Flujo actualizado con nuevos módulos
- [x] `docs/configuration.md` - Section weights, presets, aliases, fineTuning, argumentativeZoning, cache LLM, múltiples estrategias
- [x] `CHANGELOG.md` - v2.0: Section-weighted + Structure + Zoning + Fine-tuning + Captions + Multi-query strategies + Chat engine

---

## 🔄 CÓMO CONTINUAR EN PRÓXIMA SESIÓN

```bash
# 1. Verificar estado
cd /home/vant/Documentos/proyectos/LSI/SLIP
cat SESSION_STATE.md

# 2. Ejecutar pipeline completo (siempre el mismo comando)
#    - Primera pasada: usa modelo base bge-base-en-v1.5, luego genera fine-tuning
#    - Pasadas siguientes: usa modelo fine-tuneado existente o regenera si el corpus cambió
python main.py --proc SLIP

# 3. Verificar qué modelo usó discoveryEngine
#    Revisar logs para "[Discovery] Usando modelo fine-tuneado" o "[Discovery] Usando modelo base"
```

---

## ⚙️ CONFIGURACIÓN ACTUAL (config.json - sección discovery)

```json
"discovery": {
  "embedding_model": "BAAI/bge-base-en-v1.5",
  "section_filter_threshold": 0.3,
  "section_filter_alpha": 0.5,
  "off_topic_threshold": 0.6,
  "off_topic_alpha": 0.5,
  "min_topics_per_rq": 2,
  "min_papers_per_topic": 2,
  "n_seed_terms": 30,
  "synthesis_min_tokens": 300,
  "synthesis_max_tokens": 600,
  "synthesis_max_chars": 4000,
  "section_weights_preset": "technical_paper",
  "section_weight_overrides": { "abstract": 1.8 }
}
```

---

## 📌 NOTAS IMPORTANTES

1. **Backup existe:** `candidate_concepts.json.backup_section_weighted` (baseline sin section-weighted)
2. **LLM cache:** 923 traducciones cached en `translation_cache.json`
3. **Ejecución completa:** ~25 min (dominado por LLM calls en structureExtraction + trainingMaterials)
4. **GPU:** RTX 4060 Ti (7.6 GB) - usado para embeddings + LLM

---

### ✅ TAREA COMPLETADA: Límite de tópicos emergentes (max_emerging_topics)

La generación de tópicos emergentes en `trainingMaterials` estaba tomando ~20 min
para 45 tópicos (~28s c/u), lo cual es colateral para el objetivo principal del pipeline.

**Cambio:** `sources/trainingMaterials.py` + `config.json` + `results/input/<investigacion>/config.json`

- **Nuevo parámetro:** `training_materials.max_emerging_topics` (default: 20, 0 = sin límite)
- **Ubicación en config:** `config.json` → `defaults.training_materials.max_emerging_topics`
  y `results/input/<subject>/config.json` → `training_materials.max_emerging_topics`
- **Mecanismo:** `_build_emerging_topics()` aplica `slice` sobre la lista de conceptos
  emergentes antes de llamar al LLM. Si `max_emerging_topics=0` o no se especifica,
  se genera todos (comportamiento original).
- **Resultado esperado:** Con 20 tópicos, la fase pasa de ~20 min a ~9-10 min.

**Uso en config.json de investigación:**
```json
"training_materials": {
    "manuscript": 0,
    "emergente": 1,
    "max_emerging_topics": 20
}
```

Para desactivar completamente: `"emergente": 0`.

---

### Sesión cerrada — 2026-10-19

**Qué se hizo:** 
1. Se añadió proveedor API nativo para arXiv (`sources/ingestion/arxiv_provider.py`) y se registró en `sources/ingestion/manager.py` (`PROVIDER_CLASSES`). Prueba de ingesta exitosa: 5 registros canónicos.
2. Se actualizó la estrategia de búsqueda **02** en `results/input/SensoresACL/conceptsQuery.json` para Scopus, WoS e IEEE con la nueva query (3 bloques: LLMs, datos fisiológicos/series temporales, degradación/incertidumbre/calibración).
3. Se configuró `search.providers.arxiv.mode = "api"` en `config.json` global (antes era `"file"`).
4. **Parser de Google Scholar integrado** (`parse_scholar_file` en `sources/normalizeSearchResults.py`):
   - Consume `scholar_export_*.csv` (columnas: Title, Authors, Venue, Year, Publisher, DocType, Snippet, CitedBy, RelatedCount, VersionsCount)
   - `original_id` = `title:<sha1>` (Scholar no expone DOI), `abstract` = Snippet, `citation_count` = CitedBy
   - Añadido `scholar` a `search.availables` y `"scholar": "scholar_export.csv"` en `config.json`
   - Integración end-to-end exitosa: **49 papers** de `scholar_export_01.csv` → `scholar_search_01.json`, etiquetados con `query_strategies: ['01']`
   - Pipeline completo: **1783 papers normalizados** (Scopus 500, IEEE 997, PubMed 200, ACM 18, arXiv 19, Scholar 49)

**Pendiente:** Ninguno de los items abiertos en esta sesión.

**Si cambiaste estructura, módulos, config o flujos:** `AGENTS.md` actualizado con `arxiv` y `scholar` en `search.availables`, estructura de directorios con `scholar_export_*.csv`, y proveedor arXiv en modo `api`.

---

### Sesión cerrada — 2026-10-19

**Qué se hizo:** 
1. Se corrigió `enrichmentEngine.py` para que el `selection_checkpoint.json` solo se reutilice cuando las condiciones de entrada sean idénticas (mismo conjunto de `paper_ids` en `ranked_papers.json`, mismo orden).
   - Se añadió `_ranking_fingerprint()` que calcula un hash SHA-256 de los paper_ids ordenados del ranking actual.
   - Si el fingerprint del checkpoint no coincide con el actual, se descarta el checkpoint y la selección empieza desde cero.
   - Esto evita arrastrar decisiones de una ejecución anterior sobre un conjunto diferente de papers (criterio SOA).

2. Se actualizó `AGENTS.md` documentando la regla del checkpoint en la sección de puntos de interacción manual.

**Pendiente:** Ninguno de los items abiertos en esta sesión.

**Si cambiaste estructura, módulos, config o flujos:** `AGENTS.md` y `SESSION_STATE.md` actualizados con la regla del checkpoint y con el umbral de similitud en `config.json`.

---

### Sesión cerrada — 2026-10-19

**Qué se hizo:**
1. Se eliminó el valor hardcodeado `SIMILARITY_THRESHOLD = 0.82` de `rankingEngine.py` y `DEFAULT_SIMILARITY_THRESHOLD = 0.82` de `completeSelection.py`.
2. Ahora `similarity_threshold` se lee obligatoriamente de `config.json > defaults.ranking.similarity_threshold`.
3. Si no está definido en config.json, se lanza un warning claro y se aplica `0.00` (sin filtro de similitud).
4. `config.json` tiene `"similarity_threshold": 0.82` en `defaults.ranking`.
5. Se actualizó `AGENTS.md` documentando la sección `ranking.*` con `similarity_threshold`.

**Pendiente:** Ninguno de los items abiertos en esta sesión.

**Si cambiaste estructura, módulos, config o flujos:** `AGENTS.md` y `SESSION_STATE.md` actualizados con el umbral de similitud y fine-tuning incremental en `config.json`.

---

### Sesión cerrada — 2026-10-19

**Qué se hizo en esta sesión:**

1. **Proveedor API nativo para arXiv** (`sources/ingestion/arxiv_provider.py` + registro en `manager.py`). Probado exitosamente con 5 registros.
2. **Parser de Google Scholar** (`parse_scholar_file` en `normalizeSearchResults.py`). Añadido `scholar` a `search.availables`. 49 papers en strategy 01, 50 en strategy 02.
3. **Limpieza automática de archivos legacy** (`*_search.json` previos) al inicio de `normalizeSearchResults` — criterio SOA.
4. **Recuperación de abstracts universal** (`abstractRecovery.py` por DOI vía OpenAlex para cualquier fuente; `parse_scholar_file` con `abstract_truncated`).
5. **Fallback de query en `QueryResolver`**: si una fuente no tiene query específica, usa la query genérica de la primera estrategia en `conceptsQuery.json`.
6. **Log debug de URL completa** en `HttpClient.get()` cuando se agotan reintentos o hay HTTP 404.
7. **Parser de arXiv con query simplificada**: `("large language model" OR LLM OR "foundation model") AND ("physiological signal" OR biosignal OR wearable OR "vital signs")` en strategy 01 de `conceptsQuery.json`.
8. **Scopus en modo `"file"`** (no API) en `config.json` global — usa los archivos CSV exportados manualmente.
9. **Umbrall de similitud desde config.json** (no hardcodeado): `defaults.ranking.similarity_threshold = 0.72`. Si no está definido, warning + 0.00.
10. **Fine-tuning incremental**: `fineTuning.incremental: true` en `config.json` de SensoresACL. Si existe modelo previo, se usa como base en lugar de `base_model` original.
11. **Checkpoint de selección con verificación de entrada** (`enrichmentEngine.py`): `_ranking_fingerprint()` (hash SHA-256 de paper_ids ordenados). Solo se reutiliza si el conjunto de papers en el ranking es idéntico.
12. **`completeSelection.py`** (reemplaza `copySelectedPapers.py`): genera `prisma.md` con fecha/hora, criterio de comparación (string base del estudio), estrategias de búsqueda, calidad de metadatos por `fuente_estrategia`, 20 papers con más coseno fuera de top_k, flujo PRISMA resumido. Umbral ANTES del top_k.
13. **`rankingEngine.py`**: umbral de similitud aplicado ANTES del top_k. Guarda `ranking_stats.json` y `ranking_outside_top_k.json`.

**Estado del pipeline al cerrar:** Se ejecutó hasta `discoveryEngine` donde falló por tener solo 2 papers en el corpus (problema de selección previa, no de los cambios realizados). Requiere borrar `selection_checkpoint.json` y seleccionar más papers en el paso de enriquecimiento para continuar.

**Próximos pasos:** Borrar checkpoint y relanzar pipeline seleccionando más papers (≥20 recomendados) en el paso de enriquecimiento.

**Si cambiaste estructura, módulos, config o flujos:** `AGENTS.md` y `SESSION_STATE.md` actualizados.

---

### Sesión cerrada — 2026-10-20

**Qué se hizo:**

1. **Umbrall de similitud desde config.json (no hardcodeado):**
   - `rankingEngine.py` y `completeSelection.py`: eliminado `SIMILARITY_THRESHOLD` / `DEFAULT_SIMILARITY_THRESHOLD` hardcodeado.
   - Ahora se lee obligatoriamente de `config.json > defaults.ranking.similarity_threshold`.
   - Si no está definido, se lanza warning y se usa `0.00` (sin filtro).
   - `config.json` global tiene `"similarity_threshold": 0.72`.
   - `AGENTS.md` documentado en sección `ranking.*`.

2. **Fine-tuning incremental (`fineTuningEngine.py`):**
   - Añadido parámetro `incremental` en `FineTuningConfig` (default `False`).
   - `config.json` de SensoresACL: `"incremental": true`.
   - Si está habilitado y existe modelo previo (`models/slip-{subject}/`), se usa como `base_model` en lugar de `BAAI/bge-base-en-v1.5`.
   - `AGENTS.md` documentado en sección `fineTuning.*`.

3. **Recuperación de abstracts universales (`abstractRecovery.py`):**
   - Añadido `MIN_ABSTRACT_LEN = 100`.
   - Nueva función `_abstract_needs_recovery()`: detecta abstracts vacíos, con elipsis (… o ...) o menores de 100 caracteres.
   - `recover_abstracts()` usa `_abstract_needs_recovery()` en lugar de `needs_abstract_recovery()`.
   - Los abstracts cortos (ej. Scholar con ~185 caracteres) ahora se intentan recuperar vía OpenAlex por DOI.

4. **Parser Scholar con detección de abstracts cortos (`normalizeSearchResults.py`):**
   - `abstract_truncated` ahora también es `True` si `len(abstract.strip()) < MIN_ABSTRACT_LEN`.
   - Importado `MIN_ABSTRACT_LEN` desde `abstractRecovery`.
   - Aplicado en parsers de scholar, arxiv y acm.

5. **Checkpoint de selección con verificación de fingerprint (`enrichmentEngine.py`):**
   - `_ranking_fingerprint()`: hash SHA-256 de paper_ids ordenados del ranking actual.
   - Si el checkpoint no tiene fingerprint o es diferente al actual, se descarta y se empieza desde cero.
   - Ya no hay que borrar manualmente `selection_checkpoint.json`.

6. **Informe PRISMA (`completeSelection.py`):**
   - Sección 3.1 muestra el string de consulta del estudio de forma legible (objetivos, keywords, focus_terms en listas separadas, sin repeticiones por peso).
   - Sección 3.3 muestra calidad de metadatos (sin título/abstract/keywords completos) con subtotales por `fuente_estrategia` (ej: `scopus_01`, `ieee_02`).
   - Sección 3.4 muestra los 20 papers con MÁS coseno que se quedan fuera por top_k.
   - Sección 7 (flujo PRISMA) actualizada con umbral antes de top_k.
   - Cabecera con fecha y hora de ejecución.
   - Eliminado texto introductorio genérico.

7. **Consulta de arXiv simplificada (`conceptsQuery.json` strategy 01):**
   - Añadida `"ieee"` y `"arxiv"` a queries de strategy 01.
   - Query de arXiv: `("large language model" OR LLM OR "foundation model") AND ("physiological signal" OR biosignal OR wearable OR "vital signs")` — más corta para evitar timeouts.

8. **Scopus en modo `"file"` (`config.json` global):**
   - Cambiado de `"api"` a `"file"` para usar los exports CSV existentes.

9. **Filtro por dominio/URL blacklist antes del ranking (`rankingEngine.py`, `adquisitionEngine.py`, `common/utils.py`):**
   - Nueva función `load_url_blacklist()` en `sources/common/utils.py`: lee primariamente de `config.json` global (sección `url_blacklist`, aplica a todas las investigaciones) y secundariamente de `personalizacion.json` por subject (override).
   - Nueva función `is_url_blacklisted()` en `sources/common/utils.py`: comprueba `landing_page`, `pdf_url`, `source_url` contra los dominios blacklisteados.
   - `rankingEngine.py`: aplica el filtro ANTES del scoring/top_k, para que los papers de dominios inaccesibles no ocupen cupo.
   - `adquisitionEngine.py`: defensa en profundidad, aplica el mismo filtro antes de intentar descargar PDFs.
   - `config_example.json`: nueva sección `defaults.url_blacklist` con `urls` y `domains`.
   - `AGENTS.md`: documentada la sección `url_blacklist` (global) y `blacklist.*` (por subject).
   - Ejemplo de uso en config.json global: `{"url_blacklist": {"urls": ["iopscience.iop.org"], "domains": ["springer.com"]}}`.

10. **Ruta configurable para `results/` (`results_root` en `environment` de `config.json`):**
    - `paramsManager.py` (`manageEnv()`): lee `environment.results_root` del config global. Si está definido y no vacío, se usa como base para `input` y `output` en lugar de `results/` bajo `realPath`. Puede ser absoluto o relativo a `realPath`. Si está vacío, se aplica el comportamiento actual (`results/` en la raíz del proyecto).
    - `utils.py`: `load_blacklist()` y `load_url_blacklist()` usan `_base_input_dir()` que respeta `results_root`.
    - `fineTuningEngine.py`: resuelve el config de investigación usando `results_root` en lugar de hardcodear `"results"`.
    - `completeSelection.py`: usa `input_dir` resuelto por `inicioModulo()` (que ya respeta `results_root`) en lugar de `Path("results/input")`.
    - `discoveryEngine.py`: resuelve rutas de modelos usando `results_root` en lugar de hardcodear `"results"`.
    - `chatEngine.py`: `_get_output_dir(subject)` resuelve el directorio de output usando `processControl.env` / `results_root`, eliminando hardcodes de `Path("results/output/SensoresACL")`.
    - `config_example.json`: nueva clave `environment.results_root` (por defecto `""`).
    - `AGENTS.md`: documentada la sección `environment.results_root` y la regla de no hardcodear `"results"` en rutas.

**Pendiente:**
- Ninguno.

**Notas sobre el umbral de similitud (0.72):**
- Con `similarity_threshold = 0.72` se obtienen 409 papers que superan el umbral, suficientes para seleccionar 130 (top_k). No es necesario bajar el umbral.
- Las venues objetivo ahora se leen desde `conceptsQuery.json` (clave `prisma_target_venues` y `prisma_venue_aliases`) en lugar de estar hardcodeadas en `completeSelection.py`.
- El matching de venue mejorado usa aliases de nombres completos (ej. "Association for Computational Linguistics" → ACL), lo que aumenta las coincidencias detectadas.

**Si cambiaste estructura, módulos, config o flujos:** `AGENTS.md` y `SESSION_STATE.md` actualizados.

---

### Sesión cerrada — 2026-10-21

**Qué se hizo:**

1. **PRISMA removido de `training_materials.md` (`sources/trainingMaterials.py`):**
   - Eliminada sección PRISMA duplicada que ya vive exclusivamente en `prisma.md` (`completeSelection.py`).
   - Eliminados: campo `prisma` de `DocumentModel`, método `DocumentDataBuilder._build_prisma()`, dataclass `PrismaStats`, métodos `_render_prisma()` y `_render_prisma_annex()`, construcción de `prisma_raw`.
   - Mantenedo filtro de blacklist para `papers_text` y `metadata_lookup` (relevante para el contenido del documento).
   - **Errores detectados y corregidos al eliminar:** conteos de DB desde archivos legacy (sin sufijo de estrategia), `n_sought` usando `len(selected_acquired)` en lugar de papers buscados, blacklist aplicada inconsistente, `assessed_for_eligibility` con `len(papers_text)`, doble conteo por estrategia.

2. **Límite de papers incrementado (`config.json`):**
   - `defaults.ranking.top_k`: **100 → 130**.

3. **Papers manuales fuera del límite (`sources/rankingEngine.py`):**
   - Nueva función `_get_global_id()` (misma lógica que `manualIngestion.py`).
   - Nueva función `_load_manual_global_ids()` lee `manual_papers.json` y devuelve set de `global_id`.
   - `processRankingEngine()` excluye papers manuales del ranking **antes** de aplicar `top_k`, liberando cupo para papers de búsqueda adicionales.
   - Resultado: si límite=130 y hay 10 manuales, el total puede ser 140 (130 búsqueda + 10 manuales).

4. **Fix de error preexistente (`sources/completeSelection.py`):**
   - Añadido `import os` que faltaba y causaba `NameError` en `processCompleteSelection()`.

5. **Sección venues en `prisma.md` (`sources/completeSelection.py`):**
   - Nueva sección **6.5**: distribución de papers seleccionados por venue objetivo (ACL, SIGIR, ECIR, EMNLP, COLING, WWW, CKIM, CONLL, NAACL, EACL, ICTIR).
   - Matching case-insensitive por subcadena sobre el campo `venue`.

6. **Nuevo módulo: exportación para Gemini Notebook (`sources/geminiNotebookExport.py`):**
   - Genera archivos Markdown en `results/output/<subject>/notebook/`.
   - Máximo 50 papers (fuentes) por archivo, máximo 200 MB por archivo, máximo 500 000 palabras por paper.
   - Papers ordenados por `relevance_score` (más relevantes primero).
   - Formato: `TITULO DEL PAPER: ID-titulo` seguido del texto plano del paper.
   - Se integra en `main.py` como paso 21 del pipeline (tras `relatedWork`).
   - Prueba con SensoresACL: 3 cuadernos, 126 papers exportados.

7. **Blacklist de URLs (`config.json`):**
   - Añadida sección `url_blacklist` con `iopscience.iop.org` y `ascopubs.org` (en `urls` y `domains`).
   - Los papers con esos dominios se excluyen antes del ranking y como defensa en profundidad en adquisición.

8. **Ejecución dividida local ↔ servidor GPU (`sources/common/paramsManager.py`, `main.py`, `AGENTS.md`):**
   - Nuevo parámetro `--serverGPU` con valores `start` (pasos 1-7, interactiva local) y `server` (pasos 8-21, servidor GPU).
   - Si no se indica, se ejecuta el pipeline completo en local (comportamiento habitual).
   - `mainProcess()` bifurca según el valor: `start` ejecuta pasos 1-7 y se detiene; `server` ejecuta pasos 8-21; `None` ejecuta todo.
   - Al terminar `start`, se detiene con mensaje indicando al investigador subir `results/` al servidor GPU.

9. **Parámetro `--step` para inicio desde paso específico (`main.py`, `paramsManager.py`, `AGENTS.md`):**
   - Nuevo parámetro `--step <nombre>` que inicia el pipeline directamente desde el paso indicado y continúa hasta el final.
   - Ejemplos: `--step TechnicalAnnex`, `--step DiscoveryEngine`, `--step StructureExtraction`.
   - Si no se indica, se ejecuta normalmente (o según `--serverGPU`).
   - Implementado mediante `_STEP_REGISTRY` que mapea nombres de pasos a sus funciones importables dinámicamente.
   - Comparación case-insensitive del nombre del paso.
   - Útil para reanudar tras suspensión o bloqueo del equipo.

10. **Timeout de llamadas LLM (`sources/common/llm_client.py`, `config.json`):**
    - `OllamaTransport` acepta parámetro `timeout` (por defecto 120 segundos).
    - Cada llamada a `ollama.generate()` incluye `timeout=`, evitando bloqueos indefinidos si el equipo se suspende o Ollama deja de responder.
    - `config.json > defaults.llm.llm_timeout` permite configurar el timeout (por defecto 120.0).
    - `config.json > defaults.llm.llm_keep_alive` permite configurar keep_alive de Ollama (por defecto "5m").
    - Las factories `create_ollama_client()` y `create_resilient_ollama_client()` aceptan y propagan `timeout` y `keep_alive`.

11. **IEEE en modo API (`config.json`):**
    - Cambiado `search.providers.ieee.mode` de `"file"` a `"api"`.
    - La API key `3fpmh6v75fp945dztbn7xnhv` ya estaba configurada y se verificó que funciona (prueba exitosa: 12,449 registros para `"large language model"`).
    - El proveedor `sources/ingestion/ieee_provider.py` ya estaba implementado y registrado en `sources/ingestion/manager.py`.
    - A partir de ahora las búsquedas de IEEE se hacen vía API en lugar de depender de `ieee_export.csv`.

12. **Paper Blacklist con indicadores numéricos y reporte en PRISMA (`enrichmentEngine.py`, `rankingEngine.py`, `completeSelection.py`, `corpusCleaning.py`, `config.json`):**
    - Nueva estructura `paper_blacklist.entries` en config.json: cada entrada tiene `id`, `reason` (motivo) y `detail` (descripción).
    - Motivos soportados: `low_text` (bajo contenido tras limpieza → indicador 1), `operator_rejected` (operador dijo "N" en selección → indicador 2), `pdf_unreadable` (PDF no legible), etc.
    - `enrichmentEngine.py`: Nueva función `save_rejected_papers()` guarda `rejected_papers.json` cuando el operador indica "N" durante la selección interactiva.
    - `rankingEngine.py`: Nueva función `_load_paper_blacklist()` consolida config.json + rejected_papers.json + low_text_papers.json antes del ranking.
    - `completeSelection.py`: Sección **6.6** en `prisma.md` usa indicadores numéricos (1=bajo contenido, 2=excluido selección manual) con leyenda al final y título del paper en la tabla detallada, en lugar de repetir el motivo por cada entrada.
    - `corpusCleaning.py`: Ya generaba `low_text_papers.json` con papers de <500 palabras.

13. **Distribución por venue en PRISMA basada en papers > umbral (`completeSelection.py`):**
    - La sección **6.5** de `prisma.md` cuenta venues sobre todos los papers que SUPERAN el umbral de similitud (ranked_papers.json + ranking_outside_top_k.json), no solo sobre los seleccionados manualmente.
    - Se distingue entre: papers dentro de top_k y papers fuera de top_k pero > umbral.
    - Tabla con columnas: Venue / Conferencia | Papers > umbral | Papers en top_k.
    - Las venues objetivo son configurables por investigación en `results/input/{subject}/conceptsQuery.json` bajo `prisma_target_venues` y `prisma_venue_aliases`.
    - **Nuevo:** Identificación de venue por patrón de DOI mediante `doi_venue_patterns` en `conceptsQuery.json`. Cada entrada tiene `pattern`, `venue` y `category`. El proveedor `acl_anthology` extrae el venue del DOI (ej. `acl-long` en `10.18653/v1/2023.acl-long.741` → "ACL Main (Long)"). Este venue se propaga a través del merge (`searchMergeEngine`) y se usa como fallback en el matching de venues del PRISMA (`completeSelection.py`), mejorando significativamente la detección de venues objetivo cuando el campo `venue` del paper está vacío.

14. **Nuevo proveedor: ACL Anthology (`sources/ingestion/acl_anthology_provider.py`):**
    - Añadido `acl_anthology` a `search.availables` y `search.providers` en `config_example.json` y `config.json` de SensoresACL.
    - Registrado `AclAnthologyProvider` en `PROVIDER_CLASSES` de `sources/ingestion/manager.py`.
    - Usa la librería `acl_anthology` para iterar sobre el repositorio git de ACL Anthology (clonado en `sources/third_party/acl-anthology`, compartido por todas las investigaciones) y filtrar papers por query booleana (AND/OR con paréntesis) y año mínimo.
    - El repositorio **no está en git** del proyecto (excluido en `.gitignore` bajo `sources/third_party/`). Se clona manualmente en cada entorno o automáticamente por el proveedor si falta.
    - Configurable en `config.json > defaults.search.providers.acl_anthology` con `mode: "api"`, `repo_path` (opcional, por defecto `sources/third_party/acl-anthology`), `min_year` y `max_results`.
    - Si el repositorio no existe, el proveedor intenta clonarlo automáticamente (requiere `gitpython`).
    - Extrae `venue_from_doi` y `venue_category` del patrón del DOI para mejorar el matching de venues en PRISMA.
    - Añadido `acl-anthology` y `gitpython` a `requirements.txt`.
    - Las queries de ACL Anthology se añaden a `results/input/{subject}/conceptsQuery.json` bajo `query_strategies.XX.queries.acl_anthology`.

**Pendiente:** Ninguno de los items abiertos en esta sesión.

**Si cambiaste estructura, módulos, config o flujos:** `AGENTS.md` y `SESSION_STATE.md` actualizados.

---

### Sesión cerrada — 2026-10-23

**Qué salió bien:**
1. **Proveedor ACL Anthology integrado correctamente.** `sources/ingestion/acl_anthology_provider.py` creado, registrado en `manager.py` (`PROVIDER_CLASSES`), y probado end-to-end:
   - Estrategia 01 (Flujo A - señales/sensores): 96 papers encontrados
   - Estrategia 02 (Flujo B - calibración/incertidumbre/abstención): 305 papers encontrados
   - Repositorio clonado en `sources/third_party/acl-anthology/` (compartido, no versionado en git)
   - Auto-clone funcional si el repositorio no existe
2. **Venues configurables por investigación en `conceptsQuery.json`:**
   - `prisma_target_venues`: 11 venues objetivo (ACL, EMNLP, COLING, NAACL, EACL, CONLL, SIGIR, ECIR, WWW, ICTIR, CKIM)
   - `prisma_venue_aliases`: 22 aliases para matching por nombres completos
   - `doi_venue_patterns`: 10 patrones DOI→venue con venue y categoría
3. **Venue por patrón de DOI implementado:**
   - Proveedor extrae `venue_from_doi` y `venue_category` del DOI (ej. `acl-long` en `2023.acl-long.741` → "ACL Main (Long)")
   - Campo se propaga a través del merge (`searchMergeEngine`)
   - `completeSelection.py` usa `venue_from_doi` como fallback en el matching de venues del PRISMA
4. **PRISMA sección 6.5 mejorada drásticamente:** de 3 papers con venue objetivo (solo `venue` field) a **29 papers** (venue + venue_from_doi):
   - ACL: 1 → 20
   - EMNLP: 1 → 8
   - COLING: 1 → 1
5. **Umbral de similitud 0.72 confirmado:** 409 papers > umbral, suficientes para 130 top_k. No es necesario bajar el umbral.
6. **`.gitignore` actualizado:** `sources/third_party/` excluido de git.
7. **Documentación actualizada:** `AGENTS.md` y `SESSION_STATE.md` con todos los cambios.

**Qué falló:**
1. **Error inicial de serialización JSON:** `MarkupText is not JSON serializable` al guardar el snapshot de ACL Anthology. Causado por objetos `MarkupText` de la librería `acl_anthology` en los campos del paper. Solucionado convirtiendo todos los valores a `str()` explícitamente en `fetch()`.
2. **Ruta incorrecta del repositorio por defecto:** Se apuntaba a `<raíz>/third_party/` en lugar de `<raíz>/sources/third_party/`. Solucionado corrigiendo `project_root / "sources" / "third_party" / "acl-anthology"`.
3. **Snapshot corrupto:** El snapshot de estrategia 01 quedó parcialmente guardado antes del error de serialización. Solucionado eliminando el snapshot corrupto y regenerando.
4. **`venue_from_doi` no se propagaba al canonical:** `normalize_paper` en `searchMergeEngine.py` no conservaba los campos nuevos. Solucionado añadiendo `venue_from_doi` y `venue_category` a `normalize_paper()` y al dict del merge en `merge_group()`.

**Próximos pasos:**
1. Ejecutar el pipeline completo: `python main.py --subject SensoresACL --proc SLIP` (desde `corpusCleaning` en adelante, ya que los pasos 1-6 están completados)
2. Verificar que `venue_from_doi` se mantiene en `papers_metadata.json` y `papers_text.json` tras `corpusCleaning`
3. Considerar añadir tests unitarios para `AclAnthologyProvider` y `_venue_from_doi`
4. Revisar si otros proveedores (scopus, wos, ieee, etc.) también se benefician de extraer venue del DOI

**Cambios en estructura de directorios, módulos, configuración o flujos:**
- **Nuevo directorio:** `sources/third_party/` (repositorios externos clonados, excluido de git)
- **Nuevo módulo:** `sources/ingestion/acl_anthology_provider.py`
- **Nuevo archivo de config por subject:** `results/input/SensoresACL/config.json` (sección `search.providers.acl_anthology`)
- **Nuevo archivo de config por subject:** `results/input/SensoresACL/conceptsQuery.json` (claves `prisma_target_venues`, `prisma_venue_aliases`, `doi_venue_patterns`, queries `acl_anthology` en estrategias 01 y 02)
- **Archivos modificados:**
  - `sources/ingestion/manager.py` (registro de `AclAnthologyProvider`)
  - `sources/searchMergeEngine.py` (`normalize_paper` y `merge_group` con `venue_from_doi`/`venue_category`)
  - `sources/completeSelection.py` (venues configurables, matching con `venue_from_doi`)
  - `config_example.json` (`acl_anthology` en `availables` y `providers`)
  - `requirements.txt` (`acl-anthology==1.3.4`, `gitpython>=3.2.0`)
  - `.gitignore` (`sources/third_party/` excluido)
  - `AGENTS.md`, `SESSION_STATE.md` (documentación)

**Estado del pipeline al cerrar:** Se ejecutó exitosamente hasta `completeSelection` (paso extra). Los pasos 1-6 del pipeline (normalize, merge, ranking, enrichment, manual ingestion, adquisition, completeSelection) están completados para SensoresACL. Los pasos 7-21 (corpusCleaning en adelante) no se han ejecutado en esta sesión.

---

### Sesión cerrada — 2026-10-25

**Qué se hizo:**
1. **Movido `paper_blacklist.entries` del `config.json` global al config por subject de SensoresACL.**
   - Las 36 entradas de `paper_blacklist` (con `paper_id` tipo `scopus_*`, `ieee_*`, `pubmed_*`, `arxiv_*`) se eliminaron de `/config.json` (sección `defaults.paper_blacklist`).
   - Se añadieron a `results/input/SensoresACL/config.json` bajo `paper_blacklist.entries`.
   - Razón: los `paper_id` son específicos de cada investigación; dejarlos en el global mezclaría papers de diferentes investigaciones y haría el archivo inmanejable.
   - `url_blacklist` (URLs/dominios: `iopscience.iop.org`, `ascopubs.org`) se mantiene en el `config.json` global porque aplica a todas las investigaciones.
   - Ambos archivos validados como JSON correcto.
   - El código lee `paper_blacklist` desde `defaults.paper_blacklist.entries` (tras el merge profundo de `manageDefaults()`), por lo que seguirá funcionando correctamente al estar en el config de subject.

**Cambios en estructura de directorios, módulos, configuración o flujos:**
- **Archivos modificados:**
  - `config.json` (raíz): eliminada sección `defaults.paper_blacklist`.
  - `results/input/SensoresACL/config.json`: añadida sección `paper_blacklist.entries` con 36 entradas.
  - `AGENTS.md`: tabla de configuración actualizada (ubicacion de `paper_blacklist` por subject).
  - `SESSION_STATE.md`: esta entrada.

**Estado del pipeline:** Sin cambios; solo movimiento de configuración. Los pasos 1-6 de SensoresACL siguen completados hasta `completeSelection`.

---

### Sesión cerrada — 2026-10-25

**Qué se hizo:**
1. **Movido `paper_blacklist.entries` del `config.json` global al config por subject de SensoresACL.**
   - Las 36 entradas de `paper_blacklist` (con `paper_id` tipo `scopus_*`, `ieee_*`, `pubmed_*`, `arxiv_*`) se eliminaron de `/config.json` (sección `defaults.paper_blacklist`).
   - Se añadieron a `results/input/SensoresACL/config.json` bajo `paper_blacklist.entries`.
   - Razón: los `paper_id` son específicos de cada investigación; dejarlos en el global mezclaría papers de diferentes investigaciones y haría el archivo inmanejable.
   - `url_blacklist` (URLs/dominios: `iopscience.iop.org`, `ascopubs.org`) se mantiene en el `config.json` global porque aplica a todas las investigaciones.
   - Ambos archivos validados como JSON correcto.
   - El código lee `paper_blacklist` desde `defaults.paper_blacklist.entries` (tras el merge profundo de `manageDefaults()`), por lo que seguirá funcionando correctamente al estar en el config de subject.

2. **Eliminado el auto-reinicio del pipeline tras fine-tuning (`main.py`, `fineTuningEngine.py`).**
   - `main.py`: Se eliminó el bucle `while` con `max_restarts = 1` que relanzaba automáticamente el pipeline cuando `fine_tuning_generated` era True.
   - En su lugar, se muestra un aviso al final de la ejecución informando que el modelo fine-tuneado se ha regenerado y estará disponible para `discoveryEngine` en una pasada posterior.
   - `fineTuningEngine.py`: Se cambió el mensaje interno de "se recomienda reiniciar el pipeline" a "el modelo estará disponible en una pasada posterior".
   - El operador relanzará el pipeline cuando considere oportuno para aprovechar el nuevo modelo.

**Cambios en estructura de directorios, módulos, configuración o flujos:**
- **Archivos modificados:**
  - `config.json` (raíz): eliminada sección `defaults.paper_blacklist`.
  - `results/input/SensoresACL/config.json`: añadida sección `paper_blacklist.entries` con 36 entradas.
  - `main.py`: eliminado auto-reinicio tras fine-tuning; añadido aviso final.
  - `sources/fineTuningEngine.py`: mensaje interno ajustado.
  - `AGENTS.md`: tabla de ejecución (paso 13) y sección de fine-tuning actualizada.
  - `SESSION_STATE.md`: esta entrada.

**Estado del pipeline:** Sin cambios funcionales; solo se eliminó el auto-reinicio y se movió configuración. Los pasos 1-6 de SensoresACL siguen completados hasta `completeSelection`.

**Si cambiaste estructura, módulos, config o flujos:** `AGENTS.md` y `SESSION_STATE.md` actualizados.

---

### Sesión cerrada — 2026-10-25

**Qué se hizo:**
1. **Cambiado `primary_model` de Ollama en `config.json`:**
   - `primary_model`: `qwen2.5:14b` → `qwen3:14b`
   - `fallback_model`: añadido `qwen2.5:14b` como primer fallback, seguido de `qwen3:8b`, `qwen2.5:7b`, `llama3.1:8b`
2. **Añadidos iconos visuales a `writeLog()` en `sources/common/common.py`:**
   - `WARNING` → `⚠️  <mensaje>`
   - `ERROR` → `❌  <mensaje>`
   - Otros niveles → sin cambios
   - Razón: facilitar la lectura de errores y advertencias en ficheros `sbatch out` del servidor, donde no hay colores de terminal.
3. **Commit exitoso:** `77d31e0` con 35 archivos cambiados, incluyendo reparación de objetos git corruptos (`pdfIndex.py`, `ieee_provider.py`, `openalex_provider.py`, `scopus_provider.py`, `manuscriptEvidence.py`, `tests/test_enrichment.py`).

**Cambios en estructura de directorios, módulos, configuración o flujos:**
- `config.json` (raíz): sección `defaults.llm` actualizada
- `sources/common/common.py`: función `writeLog` modificada
- `AGENTS.md`: tabla de configuración LLM actualizada; nueva sección "Iconos en writeLog para logs sin color"
- `SESSION_STATE.md`: esta entrada

**Estado del pipeline:** Sin cambios funcionales; solo cambio de modelo LLM y mejora de legibilidad de logs.

**Si cambiaste estructura, módulos, config o flujos:** `AGENTS.md` y `SESSION_STATE.md` actualizados.