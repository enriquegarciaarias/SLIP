# Documentación de módulos — SLIP

Este documento resume los módulos principales del pipeline SLIP y su propósito.

## Módulos de procesamiento

| Módulo | Archivo | Responsabilidad |
|---|---|---|
| `corpusCleaning` | `sources/corpusCleaning.py` | Limpieza de corpus, extracción de texto y secciones, incl. figures_tables_captions |
| `argumentativeZoning` | `sources/argumentativeZoning.py` | Clasificación retórica de párrafos (CLAIM, EVIDENCE, METHOD, BACKGROUND, GAP, FUTURE_WORK, REST) con refiner LLM opcional |
| `structureExtraction` | `sources/structureExtraction.py` | Extracción estructurada de papers con LLM (contribuciones, metodología, resultados, etc.) + cache LLM persistente |
| `fineTuningEngine` | `sources/fineTuningEngine.py` | Fine-tuning de embeddings por investigación (auto-detección por checksum del corpus) |
| `discoveryEngine` | `sources/discoveryEngine.py` | Descubrimiento de topics por RQ con section-weighted embeddings |
| `conceptMiningEngine` | `sources/conceptMiningEngine.py` | Extracción de keywords con inyección de términos estructurados y dedup semántica |
| `conceptAlignment` | `sources/conceptAlignment.py` | Alineación de conceptos a papers |
| `conceptEvidence` | `sources/conceptEvidence.py` | Extracción de evidencias con scoring híbrido (sección × zona argumentativa) |
| `clusterEvidences` | `sources/clusterEvidences.py` | Clusterización de evidencias |
| `enrich_evidences` | `sources/enrich_evidences.py` | Enriquecimiento de evidencias |
| `sythesizeFindings` | `sources/sythesizeFindings.py` | Síntesis de hallazgos |
| `technicalAnnex` | `sources/technicalAnnex.py` | Anexo técnico con perfiles por paper |
| `trainingMaterials` | `sources/trainingMaterials.py` | Materiales de entrenamiento (PRISMA, hallazgos, tópicos emergentes) |
| `relatedWork` | `sources/relatedWork.py` | Related Work listo para paper |
| `chatEngine` | `sources/chatEngine.py` | Chat con RAG sobre literatura: preguntas en lenguaje natural sobre papers y hallazgos |

## Flujo de datos

Ver [`docs/architecture.md`](architecture.md) para el diagrama completo del pipeline y el orden de ejecución.

## Configuración

Ver [`docs/configuration.md`](configuration.md) para la referencia completa de `config.json` y la configuración por investigación (`results/input/{subject}/config.json`).

## Cambios

Ver [`../CHANGELOG.md`](../CHANGELOG.md) para el historial de cambios por versión.

## Estado del pipeline

El pipeline está funcional end-to-end. Ver `SESSION_STATE.md` en la raíz del proyecto para el estado de la última sesión y las próximas acciones prioritarias.
