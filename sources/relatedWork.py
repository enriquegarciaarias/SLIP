# generate_related_work.py
from sources.common.common import logger, processControl, writeLog
from sources.common.utils import inicioModulo

import json
from pathlib import Path
from datetime import datetime
from collections import OrderedDict


def load_findings(input_file: Path) -> list:
    """Carga concept_findings.json"""
    with open(input_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_papers_metadata(output_dir: Path) -> dict:
    """Carga papers_metadata.json para obtener DOIs y otros metadatos (opcional)."""
    metadata_file = output_dir / "papers_metadata.json"
    if not metadata_file.exists():
        return {}
    with open(metadata_file, 'r', encoding='utf-8') as f:
        metadata = json.load(f)
    # Indexar por doc_id (paper_id)
    return {p.get("paper_id"): p for p in metadata if "paper_id" in p}


def clean_text(text: str) -> str:
    """Limpia el texto para incrustarlo en markdown (elimina saltos de línea excesivos)."""
    import re
    return re.sub(r'\s+', ' ', text).strip()


def format_citation(doc_id: str, paper_title: str, metadata: dict) -> str:
    """Formatea una referencia estilo [1] con título y posiblemente DOI."""
    # Buscar DOI en metadatos si está disponible
    doi = metadata.get(doc_id, {}).get("doi", "")
    if doi:
        return f"[{doc_id}] {paper_title}. DOI: {doi}"
    else:
        return f"[{doc_id}] {paper_title}"


def generate_related_work_section(concept_data: dict, metadata: dict, ref_counter: dict) -> str:
    """
    Genera una sección de Related Work para un concepto individual,
    incluyendo citas textuales y referencias.
    ref_counter es un diccionario para llevar control de números de referencia.
    """
    concept_id = concept_data["concept_id"]
    concept_query = concept_data["concept_query"]
    findings = concept_data.get("findings", [])
    n_findings = concept_data.get("n_findings", 0)

    if n_findings == 0:
        return f"""### RQ{concept_id}. {concept_query}

**Note:** No sufficient evidence was found in the current corpus to synthesize findings for this research question. This gap may indicate a need for expanded literature coverage or query refinement.

"""

    section = f"""### RQ{concept_id}. {concept_query}

"""

    # Para cada hallazgo (normalmente 1 por concepto en tu pipeline)
    for idx, finding_data in enumerate(findings, 1):
        finding_text = finding_data["finding"]
        supporting_papers = finding_data.get("supporting_papers", [])
        top_quotes = finding_data.get("top_quotes", [])
        evidence_count = finding_data.get("evidence_count", 0)
        avg_score = finding_data.get("avg_evidence_score", 0)

        # Crear lista de referencias numéricas (usamos doc_id como número, pero podemos mapear a números secuenciales)
        # Para simplificar, usamos los doc_id originales (wos_xxx, scopus_xxx) como etiquetas.
        # Pero para mejor presentación, podemos asignar números secuenciales.
        # Vamos a mantener los doc_id como etiquetas (ya que son cortos).
        citations = [f"[{pid}]" for pid in sorted(supporting_papers)]
        citation_text = ", ".join(citations)
        if len(citations) > 2:
            citation_text = ", ".join(citations[:-1]) + f", and {citations[-1]}"

        # Párrafo de síntesis
        section += f"{citation_text} "
        # Ajustar mayúscula inicial
        if finding_text:
            section += finding_text[0].lower() + finding_text[1:] + " "
        section += f"This finding is supported by {evidence_count} evidence units (avg. similarity: {avg_score:.3f}).\n\n"

        # Añadir citas textuales relevantes
        if top_quotes:
            section += "**Supporting evidence excerpts:**\n\n"
            for quote in top_quotes[:3]:  # máximo 3 citas por hallazgo
                clean_quote = clean_text(quote['text'])
                paper_title = quote['paper_title']
                doc_id = quote['doc_id']
                section += f"> *“{clean_quote}”*  \n> — {paper_title} [{doc_id}]\n\n"

        section += "\n"

    return section


def generate_related_work_document(all_concepts: list, metadata: dict) -> str:
    """
    Genera el documento completo de Related Work
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    doc = f"""# Related Work
## Systematic Literature Review

**Generated:** {timestamp}
**Research Questions:** 5 objective concepts

---

## Introduction

This section synthesizes the state of the art across five key research questions related to human-centric AI, sensor-based human state capture, emotion detection enhancement, hallucination mitigation, and human-centric evaluation metrics. The findings are derived from a systematic analysis of the corpus using semantic retrieval and evidence clustering.

---

"""

    # Generar secciones por concepto (orden 0 a 4)
    ordered_concepts = sorted(all_concepts, key=lambda x: x["concept_id"])
    ref_counter = {}  # podría usarse para numeración secuencial

    for concept in ordered_concepts:
        doc += generate_related_work_section(concept, metadata, ref_counter)

    # Recolectar todas las referencias únicas de todos los hallazgos
    all_refs = {}
    for concept in all_concepts:
        for finding in concept.get("findings", []):
            for quote in finding.get("top_quotes", []):
                doc_id = quote['doc_id']
                title = quote['paper_title']
                if doc_id not in all_refs:
                    all_refs[doc_id] = title
            # También añadir supporting_papers si no están en quotes
            for pid in finding.get("supporting_papers", []):
                if pid not in all_refs:
                    # Buscar en quotes para obtener título
                    title = None
                    for quote in finding.get("top_quotes", []):
                        if quote['doc_id'] == pid:
                            title = quote['paper_title']
                            break
                    if title is None:
                        title = pid
                    all_refs[pid] = title

    # Sección de referencias
    doc += f"""
## References

"""
    # Ordenar por doc_id
    for doc_id in sorted(all_refs.keys()):
        title = all_refs[doc_id]
        # Buscar DOI en metadatos
        doi = metadata.get(doc_id, {}).get("doi", "")
        if doi:
            doc += f"[{doc_id}] {title}. DOI: {doi}\n\n"
        else:
            doc += f"[{doc_id}] {title}\n\n"

    doc += """
---
*This document was automatically generated by the Scientific Literature Intelligence Pipeline (SLIP). Citations should be verified against original sources.*
"""

    return doc


def processRelatedWork():
    input_dir, output_dir = inicioModulo("processRelatedWork")
    input_file = output_dir / "concept_findings.json"

    if not input_file.exists():
        writeLog("error", logger, f"[RelatedWork] No se encuentra {input_file}")
        return

    # Cargar metadatos de papers (opcional)
    metadata = load_papers_metadata(output_dir)

    # Cargar datos
    data = load_findings(input_file)

    # Generar documento
    doc = generate_related_work_document(data, metadata)

    # Guardar
    output_file = output_dir / "related_work.md"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(doc)

    writeLog("info", logger, f"[RelatedWork] Related Work guardado en {output_file}")

    # Mostrar resumen en consola
    print("\n" + "=" * 60)
    print("RESUMEN DE RELATED WORK GENERADO")
    print("=" * 60)
    for concept in data:
        concept_id = concept["concept_id"]
        n_findings = concept.get("n_findings", 0)
        if n_findings > 0:
            finding = concept["findings"][0]["finding"][:100]
            print(f"✅ RQ{concept_id}: {finding}...")
        else:
            print(f"⚠️ RQ{concept_id}: No findings")

    print(f"\n📄 Documento completo: {output_file}")


if __name__ == "__main__":
    processRelatedWork()