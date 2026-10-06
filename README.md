# SLIP — Scientific Literature Intelligence Pipeline

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-brightgreen)
![DOI](https://img.shields.io/badge/DOI-10.5281/zenodo.xxxxxx-blue)

🔬 **Pipeline secuencial de revisión de literatura** que, partiendo de resultados de búsqueda
bibliográfica, construye un corpus limpio, extrae evidencia estructurada por preguntas de
investigación y genera documentos de síntesis (trabajo relacionado, materiales de formación,
anexo técnico).

Para una descripción técnica del flujo (qué módulo consume y produce qué), ver
[`docs/pipeline_SLIP.md`](docs/pipeline_SLIP.md).

---

## 📋 1. Requisitos previos

- **Python 3.10+** (el proyecto se desarrolla con Python 3.12).
- **GPU con CUDA** recomendada (≥ 8 GB VRAM) para los modelos de embeddings y LLM locales;
  en CPU el pipeline funciona pero es mucho más lento.
- **Ollama** instalado y en ejecución (puerto por defecto `localhost:11434`), con los modelos
  LLM configurados en `config.json`:
  ```bash
  ollama pull qwen3:8b        # modelo primario
  ollama pull qwen2.5:7b      # fallback
  ollama pull llama3.1:8b     # fallback
  ```
- **Cuenta/token de Hugging Face** (los modelos de embeddings `BAAI/bge-*` se descargan de
  Hugging Face Hub). El token se coloca en `config.json > defaults.huggingFaceToken`.

---

## ⚙️ 2. Instalación

```bash
# 1. Crear y activar el entorno virtual
python3 -m venv .venv
source .venv/bin/activate

# 2. Instalar dependencias Python (desde requirements.txt generado con pip freeze)
pip install --upgrade pip
pip install -r requirements.txt

# 3. Modelos de spaCy (lematización/idiomas)
python -m spacy download en_core_web_sm
python -m spacy download es_core_news_sm

# 4. Verificar Ollama
ollama list
```

> Nota: los modelos de embeddings (`BAAI/bge-base-en-v1.5`, `BAAI/bge-large-en-v1.5`) se
> descargan automáticamente desde Hugging Face la primera vez que se ejecuta el pipeline.

---

## 🛠️ 3. Configuración

El repositorio no incluye el `config.json` real (contiene claves privadas). Debes **crearlo a
partir del ejemplo**:

```bash
cp config_example.json config.json
```

Luego editar `config.json`:

| Sección | Qué configurar |
|---|---|
| `defaults.huggingFaceToken` | Reemplazar `REEMPLAZAR_CON_TU_TOKEN_DE_HUGGING_FACE` por tu token de Hugging Face. |
| `defaults.enrichment.unpaywall_email` | Reemplazar `REEMPLAZAR_CON_TU_EMAIL` por tu email (API Unpaywall). |
| `defaults.search.availables` | Fuentes activas (`wos`, `scopus`, `ieee`, `pubmed`, `openalex`). |
| `defaults.search.<fuente>` | Nombre de fichero esperado en la entrada del subject (modo `file`, ver sección 4). |
| `defaults.search.providers` | Modo de ingesta por fuente (`api`/`file`) y credenciales (Scopus/IEEE/PubMed; OpenAlex solo `email`). |
| `defaults.search.decomposition` | `enabled: true` lanza, además de la query global, una consulta por pregunta de investigación (`concepts[]`). |
| `defaults.search.refresh` | `true` ignora el snapshot y reconsulta las APIs. |
| `defaults.search.merge` | `by_title_only_min_chars` para el dedup por título sin año. |
| `defaults.llm` | `primary_model` (qwen3:8b) y `fallback_model` (qwen2.5:7b, llama3.1:8b). |
| `defaults.<modulo>` | Umbrales de cada etapa (discovery, ranking, conceptAlignment, corpusCleaning, ...). |

---

## 📁 4. Preparación del asunto de investigación

Cada estudio se organiza por un **subject** en `results/input/{subject}/` (p. ej. `sensores`):

```
results/input/{subject}/
├── studyDescription.json     # Descripción del estudio (objetivos, keywords, focus_terms)
├── conceptsQuery.json        # Query de búsqueda + conceptos/preguntas de investigación
├── wos_export.txt            # Resultados exportados de Web of Science
├── scopus_export.csv         # Resultados exportados de Scopus
├── ieee_export.csv           # Resultados exportados de IEEE Xplore
├── pubmed_export.txt         # Resultados exportados de PubMed (formato MEDLINE)
└── manual_papers/            # (opcional) PDFs semilla + manual_papers.json
```

### Resultados de bases de datos (obligatorio)

Cada fuente se ingesta en modo **API** o modo **fichero**, según
`config.json > defaults.search.providers.<fuente>.mode`:

- **`api`** (recomendado en PubMed/Scopus/IEEE): el pipeline consulta la API oficial, guarda la
  respuesta cruda en `results/input/{subject}/_raw/` (snapshot reproducible) y la normaliza
  offline. No necesita que exportes nada a mano.
- **`file`**: consumes los ficheros exportados (comportamiento clásico; queda para WoS hasta
  disponer de API key).

Modo fichero (nombres exactos de `config.json`):

- **WoS**: exportar en *Plain text* → `wos_export.txt`
- **Scopus**: exportar en *CSV* → `scopus_export.csv`
- **IEEE Xplore**: exportar en *CSV* → `ieee_export.csv`
- **PubMed**: exportar en formato *MEDLINE* → `pubmed_export.txt`

> El parser de fichero admite múltiples ficheros por fuente si el patrón de `config.json` se
> cambia a un glob (p. ej. `pubmed_export*.txt`).

### Query de búsqueda por API

La query se lee de `conceptsQuery.json`. Como cada base de datos usa su propia sintaxis, puedes
definir queries específicas por fuente con la clave `queries`:

```json
{
  "query": "cadena genérica (WoS)",
  "queries": {
    "scopus": "TITLE-ABS-KEY(artificial intelligence AND inclusive education)",
    "ieee": "(inclusive education) AND (artificial intelligence)",
    "pubmed": "artificial intelligence AND inclusive education"
  }
}
```

Prioridad: `config.json > search.providers.<fuente>.query` → `queries.<fuente>` → `query`.
Si una fuente en modo `api` no tiene credenciales o falla, el pipeline **degrada automáticamente
a modo fichero** (si existe el export) sin abortar.

> Con `search.refresh = true` se ignora el snapshot y se vuelve a consultar la API. Por defecto
> (`false`) se reutiliza el snapshot más reciente para la misma query.

### Ingestión manual (opcional)

Para incorporar PDFs "semilla" de forma manual:

1. Crear la carpeta `results/input/{subject}/manual_papers/`.
2. Dejar los PDFs en esa carpeta.
3. Crear `manual_papers.json` con la definición de cada paper:
   ```json
   [
     { "file": "mi_paper.pdf", "title": "Título del paper", "doi": "10.xxxx/yyyy" }
   ]
   ```

> El scanner de PDFs (`manualPapersScanner`) rellena `manual_papers.json` automáticamente, por lo
> que en la práctica basta con dejar los PDFs en la carpeta.

### Manuscrito del investigador (opcional, `--manuscript 1`)

Para incorporar el manuscrito en construcción y sus referencias citadas:

```
results/input/{subject}/manual_papers/manuscript/
├── manuscript.pdf        # manuscrito (obligatorio)
├── main.tex              # fuente LaTeX (recomendado para extraer las citas)
├── biblio.bib           # bibliografía BibTeX (recomendado para el mapeo)
├── <bibkey>.pdf         # PDFs asociados, nombrados con su clave BibTeX
└── ...
```

- Los PDFs asociados se integran en el circuito normal (como `manual_papers`), quedando marcados
  con `origin="manuscript"` en `selected_papers.json` / `selected_acquired.json`.
- Nombra cada PDF con la **bibkey** de `biblio.bib` (p. ej. `smith2020.pdf`): el mapeo es
  determinista. Si no, se intenta por DOI/título y por la bóveda (`_pdf_vault`), y los no
  asociados se reportan en `manuscript_refs.json`.
- Se genera una sección **"Evidencias en el manuscrito"** en `training_materials.md` con, por
  cada referencia: el texto que la menciona, una extensión explicativa tomada del propio paper
  (LLM con grounding, caché en `manuscript_context_cache.json`) y sus datos estructurados.
- Las referencias citadas **sin PDF disponible** se marcan con ⚠️ (incluida la cita y el estado),
  para facilitar su verificación; si la bibkey no existe en el `.bib` se advierte de posible error.
- `manuscript.pdf` nunca se ingesta como paper del corpus.

---

## ▶️ 5. Ejecución

```bash
source .venv/bin/activate

# Pipeline completo para el subject "sensores"
python main.py --subject sensores --proc SLIP

# Con manuscrito del investigador y sin desarrollar conceptos emergentes
python main.py --subject sensores --proc SLIP --manuscript 1 --emergente 0
```

- `--subject`: nombre del asunto (carpeta en `results/input/`). Por defecto `sensores`.
- `--proc`: tipo de proceso. Por defecto `SLIP` (pipeline completo). Otro valor ejecuta
  `customProcess()` (complementos, p. ej. migraciones).
- `--manuscript`: `1` integra `manual_papers/manuscript/` y añade la sección de evidencias
  del manuscrito; `0` (por defecto) lo desactiva.
- `--emergente`: `0` no desarrolla los conceptos emergentes en `training_materials.md`
  (se mantienen los conteos de PRISMA y la bibliografía); `1` (por defecto) sí los desarrolla.

### Puntos de interacción manual durante la ejecución

El pipeline es **semisecuencial** y en tres momentos requiere atención del usuario:

1. **Selección de papers (enrichmentEngine):** se genera `review_original.pdf` / `review_es.pdf`
   con los candidatos y se hace una selección interactiva (aceptar/rechazar). El progreso se
   guarda en `selection_checkpoint.json` y puede reanudarse.
2. **Ingestión manual (manualIngestion):** si existe `manual_papers/`, los PDFs semilla se
   integran en `selected_papers.json`.
3. **Descarga de PDFs (adquisitionEngine):** si el sistema no puede descargar un paper,
   se muestra una **interfaz de recuperación manual** con el enlace de descarga del artículo
   (DOI, *landing page*, `pdf_url` o una búsqueda en Google Scholar). El usuario deja el PDF
   en la *papelera de recuperación* `results/output/{subject}/pdfs/manual/` (espera ~60 s por
   paper, o responde `Y`/`N`/`F`). Los PDFs descargados se archivan además en una bóveda
   (`_pdf_vault/`) para reutilizarse en futuras ejecuciones.

---

## 📦 6. Salidas principales

Todo se genera en `results/output/{subject}/`:

| Etapa | Fichero |
|---|---|
| Normalización/fusión | `*_search.json`, `canonical.json`, `ranked_papers.json` |
| Selección | `selected_papers.json`, `review_original.pdf`, `review_es.pdf` |
| Adquisición | `pdfs/`, `selected_acquired.json`, `pdf_download_log.json`, `missing_pdfs.json`, `pdf_inventory.json` |
| Corpus | `papers_metadata.json`, `papers_text.json` |
| Análisis | `candidate_concepts.json`, `concept_candidates.json`, `aligned_concepts.json`, `concept_evidence.json`, `clustered_evidences.json`, `enriched_evidences.json`, `concept_findings.json`, `technical_annex.json` |
| Manuscrito (si `--manuscript 1`) | `manuscript_refs.json`, `manuscript_context_cache.json` |
| Documentos finales | `training_materials.md`, `related_work.md` |

Logs: `ProcessLog.txt` (pipeline) y `Process.txt` (proceso).

---

## 🛡️ 7. Notas y solución de problemas

- **Ollama no responde:** asegúrate de que el servicio está levantado (`ollama serve`) y que
  los modelos de `config.json` están descargados (`ollama list`).
- **Modelos LLM con output vacío:** el cliente usa un *fallback* en cascada: si `qwen3:8b`
  devuelve vacío (agotó tokens de razonamiento), conmuta a `qwen2.5:7b` y `llama3.1:8b`.
- **PDFs no descargables:** revisar `missing_pdfs.json` y `pdf_download_log.json`; usar la
  interfaz de recuperación manual del paso 5.
- **Referencia técnica:** consultar `docs/pipeline_SLIP.md` para el detalle de entradas/salidas
  de cada módulo.

---

## 🧪 8. Tests

```bash
source .venv/bin/activate
python -m unittest discover -s tests -t .
```

Cubren las utilidades críticas: normalización de DOI, índice determinista PDF↔paper
(`pdfIndex`), filtro de referencias, cita a nivel de afirmación, dedup de conceptos,
resolución de queries por RQ y proveedor OpenAlex.
