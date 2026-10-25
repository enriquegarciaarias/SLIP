# SLIP — Estado de sesión y handoff

**Fecha:** 2026-10-10
**Rama:** main · **Último commit:** `7da914d` "Correcciones detectadas con SensorsACL"
**Working tree:** cambios **sin commitear** = sistema de *streams* (ver sección 2).
**Propósito:** punto de reanudación. Al iniciar sesión, leer este fichero:
**sección 1** = último estado, **sección 5** = próximos pasos.

---

## 0. Cómo reanudar (nota para el asistente)

El usuario suele pedir *"muéstrame la última reflexión guardada"*. Reproduce la
**sección 1** y el **estado del corpus (sección 3)**, y ofrece las opciones de la
**sección 5**. No ejecutes nada destructivo (borrar checkpoints, re-ejecutar el
pipeline) sin confirmación explícita.

Trabajo en curso: **flujo de búsqueda NLP** (segundo *stream*). El sistema de
*streams* está implementado y testeado; falta **configurar el flujo `nlp` real
para SensoresACL y lanzarlo**.

---

## 1. Último estado (2026-10-10)

Sesión centrada en SensoresACL. Se resolvieron **cinco frentes** (cuatro ya
commiteados y uno nuevo):

### A. Integración ACM/arXiv (commit `7da914d`)
- Parsers `acm`/`arxiv` en `sources/normalizeSearchResults.py`; los exports traen
  el abstract truncado (`…`).
- `sources/abstractRecovery.py`: recupera abstracts por **OpenAlex** (vía principal)
  y **API de arXiv** (fallback). arXiv se resuelve con DOI `10.48550/arXiv.<id>`.
- Config: `search.availables` + `acm`/`arxiv`, patrones `acm_export.*`/`arxiv_export.*`,
  bloque `search.abstract_recovery`.

### B. Anexo técnico — eco y canonicalización (commit `7da914d`)
- **Capa A**: `classify_echo()` detecta eco puro/subconjunto/mixto (solo ejemplos
  "discriminativos": multi-palabra o con acento); tokens neutros (EEG, AUC, WESAD)
  no disparan.
- **Capa B**: el prompt **ya no inyecta `examples`**; `canonicalize_value()` mapea
  la salida libre a etiquetas canónicas con `aliases` (inglés→español) declarados
  en `studyDescription.json`.
- Auditoría: ecos en `technical_annex.json` pasaron de **156 → 0**.

### C. Bug de DOIs de PubMed (commit `7da914d`)
- Causa: `_parse_article` iteraba `article.iter("ArticleId")` incluyendo los de la
  **lista de referencias**; el último DOI sobrescribía el real. **62%** de los
  registros PubMed tenían DOI erróneo y 342 PMCID.
- Fix: leer solo `PubmedData/ArticleIdList` (+ fallback `ELocationID`).
- Herramienta `sources/doiAudit.py` (verifica DOIs contra OpenAlex).
- Tras re-ejecutar: mismatches en `canonical.json` **276 → 1** (falso positivo por
  idioma). Test de regresión en `tests/test_ingestion.py`.

### D. Fallback de alineación doc-level (commit `7da914d`)
- Problema: los papers solo entraban en un concepto si su **tema** (BERTopic) se
  mapeaba a un RQ; con temas fragmentados se perdían papers pertinentes.
- `conceptAlignment.add_document_level_fallback()` (post-selección): asigna cada
  paper no cubierto al RQ objetivo más similar si `sim ≥ doc_fallback_threshold`.
- Resultado: recuperados **21–37 papers**; PRISMA incluidos **31 → 62 → 68**,
  excluidos **40 → 9 → 3** (los 3 restantes no generan evidencia ni consenso).
- PRISMA también corregido: cuenta papers de **consenso** como incluidos y añade
  **ACM/arXiv** a los conteos de BD (`trainingMaterials.py`).

### E. Sistema de *streams* (NUEVO — SIN COMMITEAR)
- `sources/common/streams.py`: `load_streams` / `stream_sources` /
  `stream_names_for_source`. Sin `search.streams` → flujo único `default`
  (retrocompatible).
- `QueryResolver.for_stream_source()`: prioridad `queries.<fuente>` → `query` →
  legacy.
- `ingestion/manager`: lanza las queries de cada flujo y **etiqueta** cada paper
  con `query_stream`; dedup fusiona `query_streams`; tope por flujo.
- `normalizeSearchResults`: fuentes = unión de flujos; modo file etiqueta igual.
- `searchMergeEngine`: propaga `query_streams` a `canonical.json`.
- `rankingEngine`: **ranking con cuota independiente por flujo** (cada flujo se
  rankea con su `ranking_query`/`query` y su `top_k`); imprescindible para que el
  ancla fisiológica no descarte el NLP.
- Tests: `tests/test_streams.py`. Validación sintética: un flujo `nlp` selecciona
  15 papers TSFM que en el ranking global caían fuera (solo 1/37 entraba).

### F. Vault de PDFs (NUEVO — SIN COMMITEAR)
- `sources/pdfVaultReconcile.py`: reconcilia `_pdf_vault` + `pdf_inventory.json`
  con el corpus (re-clava ficheros guardados con `global_id` erróneo por el bug C).
- Aplicado a SensoresACL: 22 re-clavados, 2 archivados, 2 duplicados eliminados.
  Inventario final: **110 entradas = 110 ficheros, 0 refs rotas, 0 duplicados**.
- 22 PDFs huérfanos de `pdfs/` movidos a backup y eliminados de trabajo
  (`pdfs/` = 72 indexados; `pdfs/manual` vacío).
- Tests: `tests/test_pdf_vault_reconcile.py`.

---

## 2. Cambios implementados (sin commitear)

Streams + vault:
- `sources/common/streams.py` (nuevo)
- `sources/ingestion/base.py` (`for_stream_source`), `sources/ingestion/manager.py`
- `sources/normalizeSearchResults.py`, `sources/searchMergeEngine.py`,
  `sources/rankingEngine.py`
- `config_example.json` (`search.streams` + `_streams_help`, sin activar)
- `sources/pdfVaultReconcile.py`, `tests/test_streams.py`,
  `tests/test_pdf_vault_reconcile.py`

**Ya commiteados en `7da914d`**: `abstractRecovery.py`, `doiAudit.py`,
`technicalAnnex.py`, `conceptAlignment.py`, `conceptEvidence.py`,
`ingestion/pubmed_provider.py`, `trainingMaterials.py`, `common/utils.py`,
`config_example.json` (base), tests `test_acm_arxiv.py`, `test_technical_annex.py`,
`test_evidence_genericity.py`, `test_ingestion.py`, `test_concepts.py`.

---

## 3. Estado del corpus y tests

- `canonical.json`: **1266 papers** (Scopus 499 · IEEE 398 · PubMed 495 · ACM 18 · arXiv 19).
- Flujo PRISMA (SensoresACL): identificados 1434 → duplicados 168 → 1266 →
  buscados 77 → no recuperados 5 → evaluados 71 → **incluidos en conceptos 68**,
  excluidos 3.
- `pdfs/`: 72 indexados · `_pdf_vault/`: 110 · `pdf_inventory.json`: 110 (consistente).
- Tests: `python -m unittest discover -s tests -t .` → **61 OK**.

Parámetros relevantes de config:
- `conceptAlignment.doc_fallback_threshold` (0.45) y `doc_fallback_enabled`.
- `search.abstract_recovery.{enabled,arxiv_fallback}`.
- `search.streams` (vacío = legacy).
- `conceptEvidence`: `generic_threshold`, `consensus_cluster_threshold`, `section_weights`.

---

## 4. Re-ejecución

Desde ingesta (incluye streams + acm/arxiv):
```bash
python main.py --subject SensoresACL --proc SLIP
```

Solo la fase de búsqueda/merge (tras cambiar `search.streams`):
```bash
.venv/bin/python - <<'PY'
from sources.common.paramsManager import getConfigs; getConfigs()
from sources.normalizeSearchResults import processNormalizeSearchResults; processNormalizeSearchResults()
from sources.searchMergeEngine import processSearchMergeEngine; processSearchMergeEngine()
from sources.rankingEngine import processRankingEngine; processRankingEngine()
PY
```

Desde alineación (sin rehacer adquisición):
```bash
.venv/bin/python - <<'PY'
from sources.common.paramsManager import getConfigs; getConfigs()
from sources.conceptAlignment import processConceptAlignment; processConceptAlignment()
from sources.conceptEvidence import processConceptEvidence; processConceptEvidence()
from sources.clusterEvidences import processClusterEvidences; processClusterEvidences()
from sources.enrich_evidences import processEnrichEvidences; processEnrichEvidences()
from sources.sythesizeFindings import processSynthesizeFindings; processSynthesizeFindings()
from sources.technicalAnnex import processTechnicalAnnex; processTechnicalAnnex()
from sources.trainingMaterials import processTrainingMaterials; processTrainingMaterials()
from sources.relatedWork import processRelatedWork; processRelatedWork()
PY
```

Reconciliar vault (tras re-ejecuciones que cambien `global_id`):
```bash
python -m sources.pdfVaultReconcile results/output/SensoresACL --dry-run
python -m sources.pdfVaultReconcile results/output/SensoresACL
```

---

## 5. Próximos pasos (prioridad)

1. **Configurar el flujo `nlp` real para SensoresACL y lanzarlo** (fase en curso):
   ```jsonc
   "search": {
     "streams": {
       "physio": { "sources": ["scopus","ieee","pubmed","wos"], "max_results": 500 },
       "nlp": {
         "sources": ["openalex","scopus","ieee","acm","arxiv"],
         "query": "large language model calibration hallucination faithfulness uncertainty evaluation",
         "queries": {
           "scopus": "TITLE-ABS-KEY(...)",
           "ieee": "(\"All Metadata\":\"...\") AND ..."
         },
         "max_results": 200,
         "top_k": 60,
         "ranking_query": "LLM reliability, calibration, hallucination, faithfulness evaluation"
       }
     }
   }
   ```
   Objetivo: cubrir el canon NLP (confianza verbalizada, ECE, predicción selectiva/conformal,
   fidelidad del CoT, razonamiento numérico y sobre series temporales) que la query
   fisiológica no captura. Ya hay material TSFM en acm/arxiv que el flujo recuperará.
   Blancos adicionales: ACL/EMNLP/NeurIPS.
2. **Commitear** el sistema de streams + vault (`sources/common/streams.py`,
   `pdfVaultReconcile.py`, cambios en base/manager/normalize/searchMergeEngine/
   rankingEngine, config_example, tests).
3. Revisar los **3 excluidos residuales** sin evidencia ni consenso
   (`ieee_58`, `ieee_6`, `pubmed_246`).
4. (Reflexión pendiente) Revisar los 54 excluidos → ya abordado vía fallback; queda
   decidir si el flujo NLP cambia el conjunto de conceptos o alimenta los 2/4/5.

---

## 6. Comandos de referencia

```bash
source .venv/bin/activate
python -m unittest discover -s tests -t .                    # tests (61 OK)
python main.py --subject SensoresACL --proc SLIP             # pipeline completo
python -m sources.doiAudit results/output/SensoresACL/canonical.json \
    --mailto TU_EMAIL --json informe.json                    # auditoría de DOIs
python -m sources.pdfVaultReconcile results/output/SensoresACL --dry-run
```

Backups de la sesión (efímeros, en `/tmp/opencode/`):
`technical_annex.pre_capa2.json`, `training_materials.pre_annex.md`,
`doi_audit_before.json`, `aligned_concepts.pre_fallback.json`,
`concept_findings.pre_fallback.json`, `pdf_inventory.pre_reconcile.json`,
`SensoresACL_pdfs_orphans/`, `SensoresACL_vault_dups/`.
