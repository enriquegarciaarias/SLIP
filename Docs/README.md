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

# Global Architecture

```text
Scopus Export
        │
        ▼
Scopus Parser
        │
        ▼
scopus_search.json

Web of Science Export
        │
        ▼
WoS Parser
        │
        ▼
wos_search.json

        ┌────────────────────┐
        │ Metadata Merger    │
        └────────────────────┘
                    │
                    ▼
            canonical.json
                    │
                    ▼
      Embedding-Based Ranking
                    │
                    ▼
          ranked_papers.json
                    │
                    ▼
          Metadata Enrichment
      (OpenAlex + Unpaywall)
                    │
                    ▼
        candidate_review.json
                    │
                    ▼
       Human Review Interface
                    │
                    ▼
        selected_papers.json
                    │
                    ▼
          PDF Acquisition
                    │
                    ▼
               pdfs/
                    │
                    ▼
      Full Text Extraction
                    │
                    ▼
      canonical_fulltext.json
                    │
                    ▼
          Corpus Cleaning
                    │
                    ▼
          clean_corpus.json
                    │
                    ▼
          Concept Discovery
                    │
                    ▼
      candidate_concepts.json
                    │
                    ▼
          Concept Selection
                    │
                    ▼
      selected_concepts.json
                    │
                    ▼
      Knowledge Extraction
                    │
                    ▼
         knowledge_base.json
                    │
                    ▼
          Evidence Matrix
                    │
                    ▼
         Scientific Synthesis
```

---

# Phase 1: Metadata Acquisition

## Objective

Retrieve publications from multiple scientific databases and normalize them into a common schema.

## Sources

* Scopus
* Web of Science

## Output

### scopus_search.json

```json
{
  "paper_id": "...",
  "doi": "...",
  "title": "...",
  "abstract": "...",
  "authors": [],
  "keywords": [],
  "year": 2025
}
```

### wos_search.json

Same canonical format.

---

# Phase 2: Metadata Merge

## Objective

Create a single canonical metadata repository without duplicates.

## Deduplication Strategy

Priority order:

1. DOI exact match
2. Normalized title exact match

## Output

```text
canonical.json
```

Each record represents one scientific publication regardless of the source database.

# Phase 3: Relevance Ranking

## Objective

Rank publications before downloading PDFs.

## Input

* studyDescription.txt
* title
* abstract
* keywords

## Method

Sentence-Transformer embeddings.

Similarity:

```text
studyDescription
          vs
title + abstract + keywords
```

Cosine similarity is used to generate a relevance score.

## Output

```text
ranked_papers.json
```

Example:

```json
{
  "paper_id": "...",
  "relevance_score": 0.81
}
```

# Phase 4: Metadata Enrichment

## Objective

Retrieve bibliometric and open-access metadata.

## Sources

### OpenAlex

Provides:

* Citation count
* OpenAlex ID
* Publication year

### Unpaywall

Provides:

* OA status
* PDF URL
* Landing page

## Output

```text
enriched_papers.json
```

# Phase 5: Candidate Review

## Objective

Support human relevance assessment before downloading PDFs.

## Outputs

### candidate_review.json

Contains:

* title
* abstract
* year
* citation_count
* relevance_score

### review_original.pdf

Top-ranked papers in original language.

### review_es.pdf

Top-ranked papers with abstracts translated into Spanish.

## Human Decision

The researcher manually selects papers.

Output:

```text
selected_papers.json
```

# Phase 6: PDF Acquisition

## Objective

Download PDFs only for manually selected papers.

## Priority Order

1. Unpaywall PDF URL
2. Open-access landing page
3. DOI resolution
4. Manual retrieval

## Outputs

```text
pdfs/
pdf_download_log.json
missing_pdfs.json
```

# Phase 7: Full Text Extraction

## Current Implementation

PyMuPDF

## Objective

Create a canonical full-text repository.

## Output

```text
canonical_fulltext.json
```

Example:

```json
{
  "paper_id": "...",
  "title": "...",
  "full_text": "...",
  "sections": {
      "abstract": "...",
      "introduction": "...",
      "methodology": "...",
      "results": "...",
      "conclusion": "..."
  }
}
```

# Phase 8: Corpus Cleaning

## Objective

Remove non-scientific noise.

Examples:

* References
* Copyright notices
* URLs
* Page numbers
* Headers
* Footers

## Output

```text
clean_corpus.json
```

# Phase 9: Concept Discovery

## Objective

Discover relevant concepts from the literature instead of imposing them a priori.

## Input

* studyDescription.txt
* clean_corpus.json

## Output

```json
{
  "concept": "Human Alignment",
  "frequency": 87,
  "papers": 42
}
```

Stored in:

```text
candidate_concepts.json
```

# Phase 10: Knowledge Extraction

## Objective

Extract structured evidence associated with selected concepts.

## Stored Information

* Findings
* Methods
* Datasets
* Metrics
* Limitations
* Future work

Each extraction must include evidence spans.

Example:

```json
{
  "concept": "Calibration",
  "finding": "...",
  "evidence": "..."
}
```

# Phase 11: Evidence Matrix



# Phase 12: Semantic Retrieval

## Objective

Ask research questions over the entire corpus.

## Examples

```text
Which papers use CLIP?
```

```text
Which papers discuss gender bias mitigation?
```

```text
Which datasets are most frequently used?
```

## Retrieval Pipeline

```text
Question
   │
Embedding
   │
Vector Search
   │
Evidence Retrieval
   │
LLM Synthesis
```

---

# Phase 13: Contradiction Detection

## Objective

Identify conflicting findings.

## Example

Paper A:

```text
CLIP improves performance.
```

Paper B:

```text
CLIP introduces bias.
```

The system flags:

```text
Potential contradiction detected.
```

## Benefits

Useful for:

* Literature reviews.
* Discussion sections.
* Research gap identification.

---

# Phase 14: Knowledge Accumulation

## Objective

Transform papers into reusable scientific knowledge.

Instead of:

```text
Paper → Summary
```

Create:

```text
Paper → Structured Knowledge
```

Example:

```json
{
  "dataset": "EXIST",
  "task": "Sexism Detection",
  "architecture": "CLIP",
  "f1_score": 0.81
}
```

Over time, the repository becomes a domain-specific research knowledge base.

---

# Recommended Technology Stack

## Metadata Layer

* SQLite

## PDF Processing

* GROBID
* PyMuPDF

## Orchestration

* Python

## LLM

Preferred:

* Qwen3-32B

Alternative:

* Gemma 3 27B

Lightweight:

* Qwen3-8B

## Embeddings

* BGE-M3

## Vector Database

* FAISS

## Interface

* Streamlit

## Outputs

* Markdown
* CSV
* XLSX
* LaTeX tables

---

# Expected Research Benefits

The proposed architecture converts literature review from a document-centric process into a knowledge-centric process.

Instead of accumulating PDFs and summaries, the researcher progressively builds:

1. A curated scientific corpus.
2. A structured evidence matrix.
3. A semantic knowledge base.
4. A citation network.
5. A reusable repository of research findings.

The resulting system supports thesis writing, systematic reviews, survey articles, research gap analysis, and evidence synthesis while maintaining traceability to the original scientific sources.

# Current Implementation Status

| Phase | Status |
|---------|---------|
| Metadata Acquisition | ✅ Implemented |
| Metadata Merge | ✅ Implemented |
| Relevance Ranking | ✅ Implemented |
| Metadata Enrichment | ✅ Implemented |
| Candidate Review | ✅ Implemented |
| Human Selection | ✅ Implemented |
| PDF Acquisition | ✅ Implemented |
| Full Text Extraction | ✅ Implemented |
| Corpus Cleaning | ⏳ Planned |
| Concept Discovery | ⏳ Planned |
| Knowledge Extraction | ⏳ Planned |
| Evidence Matrix | ⏳ Planned |
| Knowledge Base | ⏳ Planned |
| Semantic Retrieval | ⏳ Planned |