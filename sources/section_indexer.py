# section_indexer.py
import json
from pathlib import Path
from typing import Dict, List, Optional
from collections import defaultdict
import numpy as np
from sentence_transformers import SentenceTransformer


# --------------------------------------------------
# SECTION WEIGHTS & PRIORITIES
# --------------------------------------------------

SECTION_WEIGHTS = {
    "abstract":     1.0,
    "introduction": 0.9,
    "methodology":  0.8,
    "results":      0.85,
    "conclusion":   0.7,
    "discussion":   0.75,
    "limitations":  0.6,
    "related_work": 0.5,
}

CONCEPT_SECTION_PRIORITY = {
    "evaluation":  ["results", "methodology", "conclusion", "discussion"],
    "robustness":  ["methodology", "results", "introduction"],
    "cross-modal": ["methodology", "introduction", "results"],
    "metrics":     ["methodology", "results", "abstract"],
    "failures":    ["results", "conclusion", "discussion", "limitations"],
    "generation":  ["methodology", "results", "introduction"],
    "reasoning":   ["introduction", "methodology", "results"],
    "default":     ["abstract", "introduction", "conclusion"],
}


# --------------------------------------------------
# CONCEPT TYPE DETECTION
# CHANGED: moved here from conceptAlignmentEngine to keep
# concept-type logic co-located with CONCEPT_SECTION_PRIORITY.
# A single source of truth: adding a new type only requires
# updating this file, not two separate modules.
# --------------------------------------------------

CONCEPT_TYPE_PATTERNS = {
    "evaluation":  ["evaluation", "metric", "benchmark", "assessment", "measure"],
    "robustness":  ["robust", "stability", "consistency", "reliability"],
    "cross-modal": ["cross-modal", "multimodal", "vision-language", "image-text"],
    "metrics":     ["metric", "score", "rouge", "bleu", "bert", "correlation"],
    "failures":    ["fail", "error", "hallucination", "limitation", "challenge"],
    "generation":  ["generation", "caption", "summary", "response", "output"],
    "reasoning":   ["reason", "inference", "logic", "common sense"],
}

def determine_concept_type(concept: dict) -> str:
    """
    Infers a concept type from its label and keywords.
    Drives section prioritization in get_section_text_with_weights.
    Moved here from conceptAlignmentEngine so CONCEPT_TYPE_PATTERNS
    and CONCEPT_SECTION_PRIORITY stay in sync automatically.
    """
    label    = concept.get("topic_label", "").lower()
    keywords = " ".join(concept.get("keywords", [])).lower()
    combined = f"{label} {keywords}"

    for concept_type, patterns in CONCEPT_TYPE_PATTERNS.items():
        if any(p in combined for p in patterns):
            return concept_type
    return "default"


# --------------------------------------------------
# CORPUS INDEX
# CHANGED: new helper that builds a positional index
# (doc_id → paper) instead of keying by paper_id.
#
# Root cause of the original bug:
#   paper_index = {p.get("paper_id"): p for p in corpus}
#   paper = paper_index.get(doc_id, {})   ← always {}
#
# doc_id is a 0-based integer assigned by the pipeline
# (the position of the paper in clean_corpus.json).
# paper_id is an external DOI string like
# "doi:10.18653/v1/2023.findings-acl.820".
# They are completely different identifiers — the lookup
# never matched, so get_section_text_with_weights always
# received an empty dict and fell back to clean_text="".
# The ENHANCED path was silently identical to the original.
# --------------------------------------------------

def build_corpus_index(corpus: List[dict]) -> dict[int, dict]:
    """
    Returns {doc_id (int) → paper (dict)} using the list position
    as the key, which is how doc_id is assigned throughout the pipeline.
    """
    return {idx: paper for idx, paper in enumerate(corpus)}


# --------------------------------------------------
# SECTION INDEX (unchanged — kept for future use)
# --------------------------------------------------

def build_section_index(paper: dict) -> dict:
    """
    Builds a section index for a single paper.
    Currently unused in the main pipeline; prepared for a
    future persistent section index feature.
    """
    sections = paper.get("sections", {})
    index = {
        "paper_id":       paper.get("paper_id", ""),
        "title":          paper.get("title", ""),
        "sections":       {},
        "total_sections": 0,
        "has_sections":   len(sections) > 0,
    }

    for section_name, section_text in sections.items():
        if section_text and len(section_text) > 100:
            section_key = section_name.lower()
            weight = SECTION_WEIGHTS.get(section_key, 0.5)
            index["sections"][section_name] = {
                "text":       section_text,
                "word_count": len(section_text.split()),
                "weight":     weight,
                "priority":   1.0,
            }
            index["total_sections"] += 1

    return index


# --------------------------------------------------
# SECTION TEXT EXTRACTION (unchanged logic)
# --------------------------------------------------

def get_section_text_with_weights(
    paper: dict,
    concept_type: str = "default",
    max_chars_per_section: int = 3000,
) -> tuple[str, dict]:
    """
    Extracts and concatenates section text weighted by concept type.
    Priority sections for the concept type are prepended; remaining
    sections follow without duplication.
    Falls back to clean_text[:10000] if no sections are present.
    """
    sections = paper.get("sections", {})
    if not sections:
        return paper.get("clean_text", "")[:10000], {}

    priority_sections = CONCEPT_SECTION_PRIORITY.get(
        concept_type,
        CONCEPT_SECTION_PRIORITY["default"],
    )

    combined_text   = []
    section_weights = {}

    # Priority sections first
    for section_name in priority_sections:
        matched = next(
            (s for s in sections if section_name in s.lower()),
            None,
        )
        if matched:
            text = sections[matched]
            if text and len(text) > 100:
                combined_text.append(text[:max_chars_per_section])
                section_weights[matched] = SECTION_WEIGHTS.get(matched.lower(), 0.7)

    # Remaining sections (no duplicates)
    for section_name, section_text in sections.items():
        if section_name not in section_weights and len(section_text) > 100:
            combined_text.append(section_text[:max_chars_per_section])
            section_weights[section_name] = SECTION_WEIGHTS.get(section_name.lower(), 0.5)

    return " ".join(combined_text), section_weights


# --------------------------------------------------
# ENHANCED CONCEPT TEXT BUILDER
# --------------------------------------------------

def build_enhanced_concept_text(
    concept:      dict,
    corpus:       List[dict],
    concept_type: str = "default",
) -> str:
    """
    Builds the embedding text for a discovered topic using
    section-weighted paper content.

    CHANGED: corpus is now indexed by position (doc_id = list index)
    via build_corpus_index(), replacing the broken paper_id lookup.

    Before this fix:
        paper_index = {p.get("paper_id"): p for p in corpus}
        paper = paper_index.get(doc_id, {})   → always {}
        → sections never used, always fell back to doc["text"]
        → ENHANCED was identical to original

    After this fix:
        corpus_index = {0: paper0, 1: paper1, ...}
        paper = corpus_index.get(doc_id, {})   → correct paper
        → sections extracted and weighted by concept_type
    """
    parts = []

    label = concept.get("topic_label", "").strip()
    if label:
        parts.append(label)

    seen_kw = set()
    for kw in concept.get("keywords", []):
        kw_clean = kw.strip()
        if kw_clean and kw_clean not in seen_kw:
            parts.append(kw_clean)
            seen_kw.add(kw_clean)

    docs = concept.get("documents", [])
    docs_added   = 0
    max_docs     = 10

    # CHANGED: positional index instead of paper_id index
    corpus_index = build_corpus_index(corpus)

    for doc in docs:
        if docs_added >= max_docs:
            break

        doc_id = doc.get("doc_id")                      # int, e.g. 1, 14
        paper  = corpus_index.get(doc_id, {})           # now resolves correctly

        section_text, _ = get_section_text_with_weights(
            paper, concept_type, max_chars_per_section=3000
        )

        if section_text and len(section_text) > 200:
            parts.append(section_text)
            docs_added += 1
        else:
            # Fallback to pre-built text from mining stage
            text = doc.get("text", "").strip()
            if len(text) > 300:
                parts.append(text[:4000])
                docs_added += 1

    return " ".join(parts)