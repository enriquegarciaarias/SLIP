# Documentación del Módulo: `processCandidateReview`

## Propósito

Módulo para **enriquecer, visualizar y seleccionar manualmente** los papers más relevantes de una investigación. Permite al investigador revisar candidatos, ver sus abstracts traducidos al español, y tomar decisiones de inclusión/exclusión con checkpoint automático.

---

## Entradas

| Archivo | Ubicación | Formato | Descripción |
|---------|-----------|---------|-------------|
| `ranked_papers.json` | `{output_dir}/` | Lista de dicts | Papers rankeados por relevancia (salida del módulo anterior) |
| `selection_checkpoint.json` | `{output_dir}/` | JSON | Estado de selección (autogenerado, opcional) |

### Estructura de `ranked_papers.json` esperada

```json
[
  {
    "paper_id": "doi:10.xxxx",
    "title": "Título del paper",
    "abstract": "Resumen...",
    "keywords": ["keyword1", "keyword2"],
    "year": 2025,
    "relevance_score": 0.85,
    "doi": "10.xxxx"
  }
]
```
## Salidas
| Archivo | Ubicación | Formato | Descripción |
|---------|-----------|---------|-------------|
| `enriched_papers.json` | `{output_dir}/` | JSON | Papers enriquecidos con datos de OpenAlex y Unpaywall |
| `candidate_review.json` | `{output_dir}/` | JSON | Lista estructurada para revisión (ordenada por relevancia + citas) |
| `review_original.pdf` | `{output_dir}/` | PDF | Documento con abstracts en inglés (top 100 papers) |
| `review_es.pdf` | `{output_dir}/` | PDF | Documento con abstracts traducidos al español |
| `selected_papers.json` | `{output_dir}/` | JSON | Papers seleccionados por el investigador |
| `selection_checkpoint.json` | `{output_dir}/` | JSON | Checkpoint de progreso (permite reanudar) |
### Estructura de selected_papers.json:
```json
[
  {
    "paper_id": "doi:10.xxxx",
    "title": "Título",
    "abstract": "Resumen original",
    "year": 2025,
    "citation_count": 10,
    "relevance_score": 0.85,
    "is_oa": true,
    "pdf_url": "https://...",
    "selected": true
  }
]
```
### Estructura de candidate_review.json
```json
[
  {
    "paper_id": "doi:10.xxxx",
    "title": "Título",
    "abstract": "Resumen...",
    "keywords": ["..."],
    "year": 2025,
    "citation_count": 10,
    "relevance_score": 0.85,
    "is_oa": true,
    "doi": "10.xxxx",
    "pdf_url": "https://...",
    "selected": false
  }
]
```
## Proceso Principal (6 pasos)
```text
┌─────────────────────────────────────────────────────────────────┐
│                    processCandidateReview()                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  PASO 1: ENRIQUECIMIENTO                                        │
│  enrichment_engine(ranked_papers)                               │
│  └── enrich_paper(paper)                                        │
│       ├── enrich_openalex(doi) → citas, año, OA                 │
│       └── enrich_unpaywall(doi) → PDF URL, landing page         │
│  Salida: enriched_papers.json                                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  PASO 2: ESTRUCTURAR REVISIÓN                                   │
│  build_candidate_review(enriched_papers)                        │
│  └── Ordena por relevance_score + citation_count (desc)         │
│  Salida: candidate_review.json                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  PASO 3: GENERAR PDFs DUALES                                   │
│  build_dual_review(review) → original + spanish                 │
│  generate_review_pdf(original) → review_original.pdf            │
│  generate_review_pdf(spanish) → review_es.pdf                   │
│  └── wrap_text() + draw_block() para formato PDF                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  PASO 4: SELECCIÓN INTERACTIVA                                  │
│  interactive_selection(review)                                  │
│  └── Por cada paper: mostrar título, año, citas, abstract       │
│      Input usuario: Y (sí) / N (no) / S (stop)                  │
│  └── Checkpoint automático después de cada decisión             │
│  Salidas: selected[], rejected[]                                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  PASO 5: GUARDAR SELECCIÓN                                      │
│  save_selected(selected) → selected_papers.json                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  PASO 6: RETORNAR RESUMEN                                       │
│  return {                                                       │
│    "selected": "ruta/selected_papers.json",                     │
│    "total_selected": N                                          │
│  }                                                              │
└─────────────────────────────────────────────────────────────────┘
```
### Diagrama de flujo simplificado
```text
ranked_papers.json
       │
       ▼
┌──────────────────┐
│  ENRIQUECIMIENTO  │  ← APIs: OpenAlex + Unpaywall
│  (DOI → metadatos)│
└──────────────────┘
       │
       ▼
enriched_papers.json
       │
       ▼
┌──────────────────┐
│  ORDENACIÓN       │  ← relevance_score + citation_count
│  (build_review)   │
└──────────────────┘
       │
       ▼
candidate_review.json
       │
       ├──────────────────┐
       ▼                  ▼
┌──────────────┐   ┌──────────────┐
│ review_      │   │ review_      │
│ original.pdf │   │ es.pdf       │
└──────────────┘   └──────────────┘
       │
       ▼
┌──────────────────────────────────┐
│  SELECCIÓN INTERACTIVA (CLI)     │
│  Usuario: Y / N / S              │
│  Checkpoint automático           │
└──────────────────────────────────┘
       │
       ▼
selected_papers.json
```
## Subprocesos importantes
1. Enriquecimiento (enrich_paper)
API	Datos obtenidos
OpenAlex	citation_count, publication_year, is_oa
Unpaywall	pdf_url (PDF gratuito si OA), landing_page
2. Generación de PDF (generate_review_pdf)
Wrap text: Ajusta texto al ancho de página

Page break automático: Detecta si queda espacio suficiente

Top 100 papers: Solo los primeros 100 (por razones de tamaño)

3. Selección interactiva (interactive_selection)
Checkpoint: Guarda progreso después de CADA decisión

Reanudación: Si se interrumpe, retoma desde el último índice

Comandos: Y (incluir), N (excluir), S (guardar y salir)

## Configuraciones importantes
| Constante | Valor | Propósito |
|-----------|-------|-----------|
| `UNPAYWALL_EMAIL` | `"ega3646209@gmail.com"` | Email para API de Unpaywall |
| `TIMEOUT` | 20 segundos | Timeout de requests a APIs |
| `SLEEP` | 0.2 segundos | Rate limiting entre requests |
| `top_k` en PDF | 100 | Máximo de papers en el PDF |

## Observaciones de diseño
Puntos fuertes:

✅ Checkpoint persistente - Permite reanudar selección sin pérdida

✅ PDFs bilingües - Abstracts en inglés y español

✅ Rate limiting - Respeta APIs gratuitas

✅ Fallback seguro - Si falla traducción, conserva original

Puntos a considerar:

⚠️ GoogleTranslator - Dependencia de API externa, puede fallar por límites

⚠️ Unpaywall requiere email - Configurado con email fijo

⚠️ SELECCIÓN INTERACTIVA - Requiere supervisión humana (no automatable)