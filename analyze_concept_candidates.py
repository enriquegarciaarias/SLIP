#!/usr/bin/env python3
"""
Compare concept_candidates.json before and after structured data integration.
"""
import json
from pathlib import Path

# Load both files - need to check if we have a backup
before_file = Path("/home/vant/Documentos/proyectos/LSI/SLIP/results/output/SensoresACL/concept_candidates.json.backup")
after_file = Path("/home/vant/Documentos/proyectos/LSI/SLIP/results/output/SensoresACL/concept_candidates.json")

# Check if backup exists
if not before_file.exists():
    # Try to find a backup
    backups = list(Path("/home/vant/Documentos/proyectos/LSI/SLIP/results/output/SensoresACL").glob("concept_candidates.json*"))
    print("Available backups:", [f.name for f in backups])
    # The current file is the "after" - we need the before from before structured integration
    # Let's check if there's a backup from the section-weighted run
    before_file = Path("/home/vant/Documentos/proyectos/LSI/SLIP/results/output/SensoresACL/concept_candidates.json.backup_section_weighted")
    # No, that's candidate_concepts. Let me just analyze the current output quality
    print("No previous backup found for concept_candidates.json, analyzing current output...")

with open(after_file, "r", encoding="utf-8") as f:
    after = json.load(f)

print("=" * 80)
print("ANÁLISIS DE CONCEPT_CANDIDATES.JSON (CON DATOS ESTRUCTURADOS)")
print("=" * 80)

# Analyze keywords quality
total_keywords = 0
structured_keywords = 0
sensor_keywords = 0
numeric_keywords = 0

# Keywords que parecen venir de datos estructurados
structured_indicators = [
    "table", "figure", "r=", "p=", "kappa", "cohen", "variance", "accuracy", 
    "f1", "auc", "precision", "recall", "sensitivity", "specificity",
    "dataset", "baseline", "metric", "signal", "modality", "approach",
    "architecture", "convolutional", "lstm", "transformer", "bert", "gpt",
    "eeg", "ecg", "emg", "hrv", "ppg", "accelerometer", "gyroscope"
]

for concept in after["concepts"]:
    keywords = concept.get("keywords", [])
    total_keywords += len(keywords)
    
    for kw in keywords:
        kw_lower = kw.lower()
        # Check if keyword looks like structured data
        if any(ind in kw_lower for ind in ["table", "figure", "r=", "p=", "kappa", "variance", "accuracy", "f1=", "auc="]):
            structured_keywords += 1
        # Check for sensor/signal terms
        if any(ind in kw_lower for ind in ["eeg", "ecg", "emg", "hrv", "ppg", "accelerometer", "gyroscope", "biosignal", "physiological"]):
            sensor_keywords += 1
        # Check for numeric/metric terms
        if any(c.isdigit() for c in kw) and any(c in kw for c in [".", "%", "=", ">"]):
            numeric_keywords += 1

print(f"\n📊 ESTADÍSTICAS DE KEYWORDS:")
print(f"  Total concepts: {after['n_concepts']}")
print(f"  Total keywords: {total_keywords}")
print(f"  Keywords con indicadores estructurados: {structured_keywords} ({structured_keywords/total_keywords*100:.1f}%)")
print(f"  Keywords sensor/physiological: {sensor_keywords} ({sensor_keywords/total_keywords*100:.1f}%)")
print(f"  Keywords numéricos/métricas: {numeric_keywords} ({numeric_keywords/total_keywords*100:.1f}%)")

# Show sample concepts with structured keywords
print(f"\n🔍 EJEMPLOS DE CONCEPTOS CON KEYWORDS ESTRUCTURADOS:")
for concept in after["concepts"][:5]:
    keywords = concept.get("keywords", [])
    struct_kw = [k for k in keywords if any(ind in k.lower() for ind in ["table", "r=", "p=", "kappa", "accuracy", "f1", "auc", "variance", "citation", "dataset", "baseline"])]
    if struct_kw:
        print(f"  Topic {concept['topic_id']}: {concept['topic_label'][:50]}")
        print(f"    Structured keywords: {struct_kw[:10]}")

# Check structured_terms in documents
print(f"\n📄 ANÁLISIS DE STRUCTURED_TERMS EN DOCUMENTOS:")
total_docs = 0
docs_with_structured = 0
total_structured_terms = 0
for concept in after["concepts"]:
    for doc in concept.get("documents", []):
        total_docs += 1
        st = doc.get("structured_terms", [])
        if st:
            docs_with_structured += 1
            total_structured_terms += len(st)

print(f"  Total documentos: {total_docs}")
print(f"  Documentos con structured_terms: {docs_with_structured} ({docs_with_structured/total_docs*100:.1f}%)")
print(f"  Promedio structured_terms por doc: {total_structured_terms/max(docs_with_structured,1):.1f}")

# Show sample structured terms
for concept in after["concepts"][:3]:
    for doc in concept.get("documents", [])[:2]:
        st = doc.get("structured_terms", [])
        if st:
            print(f"  Doc {doc['doc_id']}: {len(st)} terms")
            for s in st[:5]:
                print(f"    - {s[:100]}")
            break

print(f"\n✅ Análisis completado.")