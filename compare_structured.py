#!/usr/bin/env python3
"""
Compare candidate_concepts.json before and after structured data integration.
"""
import json
from pathlib import Path

# Load both files
before_file = Path("/home/vant/Documentos/proyectos/LSI/SLIP/results/output/SensoresACL/candidate_concepts.json.backup_section_weighted")
after_file = Path("/home/vant/Documentos/proyectos/LSI/SLIP/results/output/SensoresACL/candidate_concepts.json")

with open(before_file, "r", encoding="utf-8") as f:
    before = json.load(f)

with open(after_file, "r", encoding="utf-8") as f:
    after = json.load(f)

print("=" * 80)
print("COMPARACIÓN: ANTES (section-weighted) vs DESPUÉS (structured integration)")
print("=" * 80)

# General metrics
print(f"\n📊 MÉTRICAS GENERALES:")
print(f"  {'Métrica':<30} {'ANTES':>10} {'DESPUÉS':>10} {'Δ':>8}")
print(f"  {'-'*30} {'-'*10} {'-'*10} {'-'*8}")
print(f"  {'Documentos cubiertos':<30} {before['n_documents']:>10} {after['n_documents']:>10} {after['n_documents']-before['n_documents']:>+8}")
print(f"  {'Topics descubiertos':<30} {before['n_topics']:>10} {after['n_topics']:>10} {after['n_topics']-before['n_topics']:>+8}")

# Study similarity stats
before_sims = [c.get("study_similarity", 0) for c in before["concepts"]]
after_sims = [c.get("study_similarity", 0) for c in after["concepts"]]
print(f"  {'Study similarity (mean)':<30} {sum(before_sims)/len(before_sims):>10.4f} {sum(after_sims)/len(after_sims):>10.4f} {(sum(after_sims)/len(after_sims))-(sum(before_sims)/len(before_sims)):>+8.4f}")
print(f"  {'Study similarity (max)':<30} {max(before_sims):>10.4f} {max(after_sims):>10.4f} {max(after_sims)-max(before_sims):>+8.4f}")

# By RQ
print(f"\n📋 CONCEPTOS POR RQ:")
before_by_rq = {}
after_by_rq = {}
for c in before["concepts"]:
    rq = c.get("source_rq", "unknown")[:70]
    before_by_rq[rq] = before_by_rq.get(rq, 0) + 1
for c in after["concepts"]:
    rq = c.get("source_rq", "unknown")[:70]
    after_by_rq[rq] = after_by_rq.get(rq, 0) + 1

all_rqs = set(list(before_by_rq.keys()) + list(after_by_rq.keys()))
for rq in sorted(all_rqs):
    o = before_by_rq.get(rq, 0)
    n = after_by_rq.get(rq, 0)
    print(f"  {rq}")
    print(f"    ANTES: {o:2d} | DESPUÉS: {n:2d} | Δ: {n-o:+3d}")

# Top 5 concepts by similarity
print(f"\n🔍 TOP 5 CONCEPTOS (por study_similarity):")
before_top5 = sorted(before["concepts"], key=lambda x: x.get("study_similarity", 0), reverse=True)[:5]
after_top5 = sorted(after["concepts"], key=lambda x: x.get("study_similarity", 0), reverse=True)[:5]

print(f"\n  ANTES:")
for i, c in enumerate(before_top5):
    print(f"    {i+1}. [{c.get('study_similarity', 0):.4f}] {c.get('label', '')[:65]} (papers: {c.get('n_papers', 0)})")

print(f"\n  DESPUÉS:")
for i, c in enumerate(after_top5):
    print(f"    {i+1}. [{c.get('study_similarity', 0):.4f}] {c.get('label', '')[:65]} (papers: {c.get('n_papers', 0)})")

# Papers coverage
before_papers = set()
for c in before["concepts"]:
    before_papers.update(c.get("document_indices", []))
after_papers = set()
for c in after["concepts"]:
    after_papers.update(c.get("document_indices", []))

print(f"\n📄 COBERTURA DE PAPERS:")
print(f"  ANTES: {len(before_papers)} papers únicos")
print(f"  DESPUÉS: {len(after_papers)} papers únicos")
print(f"  En común: {len(before_papers & after_papers)}")
print(f"  Solo en DESPUÉS: {len(after_papers - before_papers)}")
print(f"  Solo en ANTES: {len(before_papers - after_papers)}")

# New concepts in AFTER (not in BEFORE by label similarity)
print(f"\n🆕 CONCEPTOS EN DESPUÉS NO PRESENTES EN ANTES (por label):")
before_labels = {c.get("label", "").lower() for c in before["concepts"]}
after_sorted = sorted(after["concepts"], key=lambda x: x.get("study_similarity", 0), reverse=True)
new_count = 0
for c in after_sorted:
    label = c.get("label", "").lower()
    if label not in before_labels:
        is_new = True
        for bl in before_labels:
            if len(set(label.split()) & set(bl.split())) > 2:
                is_new = False
                break
        if is_new:
            new_count += 1
            print(f"    [{c.get('study_similarity', 0):.4f}] {c.get('label', '')[:70]} (papers: {c.get('n_papers', 0)})")
            if new_count >= 15:
                break

# Check if seed terms included structured data
print(f"\n🔍 ANÁLISIS DE SEMILLAS (seeds):")
print(f"  Los seeds ahora deberían incluir términos de:")
print(f"  - contributions: approach, architecture, datasets, signal_modalities, metrics")
print(f"  - methodology: approach, architecture, datasets, baselines, metrics, signal_modalities")
print(f"  - results: main, ablation, key_numbers")

print(f"\n✅ Comparación completada.")