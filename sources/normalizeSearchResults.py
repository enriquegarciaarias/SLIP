#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
normalizeSearchResults.py

Input:
    input/wos_export.txt (WoS)
    input/scopus_export.csv (Scopus)
    input/ieee_export.csv (IEEE Xplore)
    input/pubmed_export.txt (PubMed)
    ...

Output:
    output/wos_search.json
    output/scopus_search.json
    output/ieee_search.json
    output/pubmed_search.json
    ...

Cada paper recibe un ID único por fuente: {source}_{n}
donde n es un entero incremental (1, 2, 3...)
"""

from sources.common.common import logger, processControl, writeLog
from sources.common.dataManager import save_json
from sources.common.utils import safe_int, normalized_title, sha1, normalize_text, normalize_doi

import csv
import glob
import json
import os
import re
from pathlib import Path
import urllib.parse
import hashlib


# ==========================================================
# Common utilities
# ==========================================================

def build_source_paper_id(source: str, index: int) -> str:
    """
    Construye ID único por fuente: {source}_{n}
    Ejemplos: wos_1, scopus_42, ieee_7, pubmed_3
    """
    return f"{source}_{index}"


def split_keywords(text):
    if not text:
        return []

    separators = [";", "|", ","]

    for sep in separators:
        if sep in text:
            return [
                k.strip()
                for k in text.split(sep)
                if k.strip()
            ]

    return [text.strip()]


def clean_ieee_abstract(abstract: str) -> str:
    """
    Limpia el abstract de IEEE Xplore eliminando prefijos comunes.
    """
    if not abstract:
        return ""

    prefixes = ["Abstract:", "Abstract—", "Abstract :", "Abstract -", "Abstract ", "Abstract"]

    for prefix in prefixes:
        if abstract.startswith(prefix):
            abstract = abstract[len(prefix):].strip()
            break

    return abstract


def extract_pubmed_id(record_text: str, field: str) -> str:
    """Extrae un campo específico del formato PubMed."""
    pattern = rf"^{re.escape(field)}\s+(.+?)$"
    for line in record_text.split('\n'):
        match = re.match(pattern, line.strip())
        if match:
            return match.group(1).strip()
    return ""


# ==========================================================
# WoS parser
# ==========================================================

def parse_wos_file(filepath, source_name="wos"):
    """
    Parsea archivo WoS y asigna IDs incrementales.
    """
    papers = []
    index = 1

    with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as f:
        reader = csv.DictReader(f, delimiter="\t")

        for row in reader:
            title = normalized_title(row.get("TI", ""))
            abstract = normalize_text(row.get("AB", ""))
            doi = normalize_doi(row.get("DI", ""))
            authors = [
                a.strip()
                for a in (row.get("AU", "") or "").split(";")
                if a.strip()
            ]

            keywords = [
                k.strip()
                for k in (row.get("DE", "") or "").split(";")
                if k.strip()
            ]

            try:
                year = int(row.get("PY", ""))
            except (ValueError, TypeError):
                year = None

            paper = {
                "paper_id": build_source_paper_id(source_name, index),
                "source": source_name,
                "source_index": index,
                "original_id": f"doi:{doi.lower().strip()}" if doi else f"title:{sha1(title)}",
                "doi": doi,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "keywords": keywords,
                "year": year,
                "venue": normalize_text(row.get("SO", "")),
                "document_type": normalize_text(row.get("DT", "")),
                "language": normalize_text(row.get("LA", "")),
                "references_count": safe_int(row.get("NR")),
                "citation_count": safe_int(row.get("TC")),
                "relevance_score": None,
                "selected": False,
                "has_full_text": False,
                "raw_source_file": os.path.basename(filepath),
                "pdf_url": None,
                "landing_page": None
            }

            papers.append(paper)
            index += 1

    return papers


# ==========================================================
# Scopus parser
# ==========================================================

def parse_scopus_file(filepath, source_name="scopus"):
    """
    Parsea archivo Scopus y asigna IDs incrementales.
    """
    papers = []
    index = 1

    with open(filepath, "r", encoding="utf-8", errors="ignore") as csvfile:
        reader = csv.DictReader(csvfile)

        for row in reader:
            title = normalized_title(row.get("Title", ""))
            doi = normalize_doi(row.get("DOI", ""))
            abstract = normalize_text(row.get("Abstract", ""))

            authors = [
                a.strip()
                for a in row.get("Author full names", "").split(";")
                if a.strip()
            ]

            keywords = []
            author_keywords = row.get("Author Keywords", "")
            index_keywords = row.get("Index Keywords", "")

            keywords.extend(split_keywords(author_keywords))
            keywords.extend(split_keywords(index_keywords))
            keywords = sorted(list(set(keywords)))

            url = row.get("Link", "")

            try:
                year = int(row.get("Year", "")) if row.get("Year") else None
            except (ValueError, TypeError):
                year = None

            paper = {
                "paper_id": build_source_paper_id(source_name, index),
                "source": source_name,
                "source_index": index,
                "original_id": f"doi:{doi.lower().strip()}" if doi else f"title:{sha1(title)}",
                "doi": doi,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "author_ids": [
                    x.strip()
                    for x in row.get("Author(s) ID", "").split(";")
                    if x.strip()
                ],
                "keywords": keywords,
                "year": year,
                "venue": row.get("Source title", ""),
                "document_type": row.get("Document Type", ""),
                "language": row.get("Language of Original Document", "unknown"),
                "references_count": None,
                "citation_count": safe_int(row.get("Cited by")),
                "open_access": row.get("Open Access", ""),
                "source_url": urllib.parse.quote(url, safe=":/"),
                "relevance_score": None,
                "selected": False,
                "has_full_text": False,
                "raw_source_file": os.path.basename(filepath),
                "pdf_url": None,
                "landing_page": None
            }

            papers.append(paper)
            index += 1

    return papers


# ==========================================================
# IEEE Xplore parser
# ==========================================================

def parse_ieee_file(filepath, source_name="ieee"):
    """
    Parsea archivo IEEE Xplore CSV y asigna IDs incrementales.
    """
    papers = []
    index = 1

    with open(filepath, "r", encoding="utf-8", errors="ignore") as csvfile:
        reader = csv.DictReader(csvfile)

        for row in reader:
            title = normalized_title(row.get("Document Title", ""))
            abstract = clean_ieee_abstract(normalize_text(row.get("Abstract", "")))
            doi = normalize_doi(row.get("DOI", ""))
            pdf_url = row.get("PDF Link", "")

            authors_raw = row.get("Authors", "")
            authors = [
                a.strip()
                for a in authors_raw.split(";")
                if a.strip()
            ]

            keywords_raw = row.get("Author Keywords", "")
            keywords = split_keywords(keywords_raw)

            ieee_terms = row.get("IEEE Terms", "")
            if ieee_terms:
                ieee_keywords = split_keywords(ieee_terms)
                keywords = list(set(keywords + ieee_keywords))

            keywords = sorted(keywords)

            try:
                year = int(row.get("Publication Year", ""))
            except (ValueError, TypeError):
                year = None

            citation_count = safe_int(row.get("Article Citation Count", ""))

            venue = row.get("Publication Title", "")
            if not venue:
                venue = row.get("Conference Name", "")

            issn = row.get("ISSN", "")
            isbn = row.get("ISBNs", "")

            meeting_date = row.get("Meeting Date", "")
            online_date = row.get("Online Date", "")

            paper = {
                "paper_id": build_source_paper_id(source_name, index),
                "source": source_name,
                "source_index": index,
                "original_id": f"doi:{doi.lower().strip()}" if doi else f"title:{sha1(title)}",
                "doi": doi,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "keywords": keywords,
                "year": year,
                "venue": venue,
                "document_type": row.get("Document Identifier", "journal"),
                "language": "English",
                "references_count": safe_int(row.get("Reference Count", "")),
                "citation_count": citation_count,
                "relevance_score": None,
                "selected": False,
                "has_full_text": False,
                "raw_source_file": os.path.basename(filepath),
                "pdf_url": pdf_url if pdf_url else None,
                "landing_page": f"https://ieeexplore.ieee.org/document/{doi.split('/')[-1]}" if doi else None,
                "ieee_terms": split_keywords(ieee_terms) if ieee_terms else [],
                "issn": issn if issn else None,
                "isbn": isbn if isbn else None,
                "meeting_date": meeting_date if meeting_date else None,
                "online_date": online_date if online_date else None
            }

            papers.append(paper)
            index += 1

    return papers


# ==========================================================
# PubMed parser (NUEVO)
# ==========================================================

def parse_pubmed_file(filepath, source_name="pubmed"):
    """
    Parsea archivo PubMed en formato estándar con etiquetas (PMID-, TI -, AB -, etc.)
    """
    papers = []
    index = 1
    skipped_count = 0

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # Dividir en registros por líneas en blanco
    raw_records = re.split(r'\n\s*\n', content)

    for record in raw_records:
        if not record.strip():
            continue

        # Diccionario para almacenar campos
        fields = {}
        lines = record.split('\n')
        current_key = None
        current_value = []

        for line in lines:
            line = line.rstrip()
            if not line:
                continue

            # Detectar etiquetas como "PMID-", "TI -", "AB -", etc.
            match = re.match(r'^([A-Za-z]{2,4})\s*[-–]\s*(.*)$', line)
            if match:
                # Guardar campo anterior si existe
                if current_key:
                    fields[current_key] = ' '.join(current_value).strip()
                current_key = match.group(1).upper()
                current_value = [match.group(2).strip()]
            else:
                # Continuación de un campo (puede tener sangría o no)
                if current_key:
                    current_value.append(line.strip())

        # Guardar el último campo
        if current_key:
            fields[current_key] = ' '.join(current_value).strip()

        # --- Extraer campos relevantes ---
        # Título
        title = normalized_title(fields.get('TI', ''))
        # Abstract
        abstract = normalize_text(fields.get('AB', ''))
        # DOI
        doi = normalize_doi(fields.get('LID', '').split(' ')[0] if fields.get('LID') else '')
        # PMID
        pmid = fields.get('PMID', '')
        # PMCID
        pmcid = fields.get('PMC', '')
        # Año (de DP: fecha de publicación)
        year = None
        dp = fields.get('DP', '')
        if dp:
            # Buscar año en formato YYYY-MM-DD o YYYY
            year_match = re.search(r'(\d{4})', dp)
            if year_match:
                year = int(year_match.group(1))
        # Autores (FAU o AU)
        authors = []
        # Los autores pueden estar en múltiples líneas con etiqueta FAU o AU
        # En este formato, suelen estar como FAU - Apellido, Nombre
        # Recorremos todas las líneas para buscar FAU/AU
        author_lines = []
        for line in record.split('\n'):
            if re.match(r'^(FAU|AU)\s*[-–]\s*(.*)$', line):
                author_lines.append(line)
        # Extraer nombres
        for aline in author_lines:
            match = re.match(r'^(FAU|AU)\s*[-–]\s*(.*)$', aline)
            if match:
                author = match.group(2).strip()
                if author:
                    authors.append(author)
        # Keywords (de MH o MESH)
        keywords = []
        mesh_lines = []
        for line in record.split('\n'):
            if re.match(r'^MH\s*[-–]\s*(.*)$', line):
                mesh_lines.append(line)
        for mline in mesh_lines:
            match = re.match(r'^MH\s*[-–]\s*(.*)$', mline)
            if match:
                term = match.group(1).strip()
                if term and term not in keywords:
                    keywords.append(term)
        # Venue (journal) desde TA o JT
        venue = fields.get('TA', fields.get('JT', ''))

        # --- Validación ---
        if not title or len(title) < 10:
            writeLog("warning", logger, f"[PubMed] Skipping record {index}: missing or too short title")
            skipped_count += 1
            index += 1
            continue

        if not abstract or len(abstract) < 50:
            writeLog("warning", logger, f"[PubMed] Skipping record {index}: missing or too short abstract (title: {title[:50]}...)")
            skipped_count += 1
            index += 1
            continue

        # Crear landing page
        landing_page = None
        if doi:
            landing_page = f"https://doi.org/{doi}"
        elif pmid:
            landing_page = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

        paper = {
            "paper_id": build_source_paper_id(source_name, index),
            "source": source_name,
            "source_index": index,
            "original_id": f"doi:{doi.lower()}" if doi else f"pmid:{pmid}",
            "doi": doi,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "keywords": keywords,
            "year": year,
            "venue": venue,
            "document_type": "article",
            "language": "English",
            "references_count": None,
            "citation_count": None,
            "relevance_score": None,
            "selected": False,
            "has_full_text": False,
            "raw_source_file": os.path.basename(filepath),
            "pdf_url": None,
            "landing_page": landing_page,
            "pmid": pmid,
            "pmcid": pmcid,
            "publication_date": dp,
            "source_fields": fields  # opcional, para depuración
        }

        papers.append(paper)
        index += 1

    writeLog("info", logger, f"[PubMed] Parsed {len(papers)} papers (skipped {skipped_count} incomplete)")
    return papers


# ==========================================================
# Parser genérico (para futuras fuentes)
# ==========================================================

def parse_generic_file(filepath, source_name, parser_type="csv"):
    """
    Parser genérico para añadir nuevas fuentes fácilmente.
    """
    writeLog("warning", logger, f"Parser genérico para {source_name} no implementado")
    return []


# ==========================================================
# Función principal unificada
# ==========================================================

def process_source(known_source: str, file_pattern: str, input_dir: Path, output_dir: Path):
    """
    Procesa una fuente de datos según su tipo.
    """
    source_files = list(glob.glob(str(input_dir / file_pattern)))

    if not source_files:
        writeLog("warning", logger, f"[{known_source.upper()}] No files found matching: {file_pattern}")
        return None

    source_file = source_files[0]
    writeLog("info", logger, f"[{known_source.upper()}] Processing: {source_file}")

    if known_source == "wos":
        data = parse_wos_file(source_file, known_source)
    elif known_source == "scopus":
        data = parse_scopus_file(source_file, known_source)
    elif known_source == "ieee":
        data = parse_ieee_file(source_file, known_source)
    elif known_source == "pubmed":
        data = parse_pubmed_file(source_file, known_source)
    else:
        data = parse_generic_file(source_file, known_source)

    if data:
        output_file = output_dir / f"{known_source}_search.json"
        save_json(data, output_file)
        writeLog("info", logger, f"[{known_source.upper()}] {len(data)} papers saved to {output_file}")
        return data

    return None


# ==========================================================
# Main
# ==========================================================

def processNormalizeSearchResults():
    """
    Procesa todas las fuentes definidas en processControl.defaults.get("search")
    """
    writeLog("info", logger, "🚀 [START] Processing normalizeSearchResults")
    base_input_dir = Path(processControl.env.get("input", ""))
    base_output_dir = Path(processControl.env.get("output", ""))
    subject = processControl.args.subject
    input_dir = base_input_dir / subject
    output_dir = base_output_dir / subject
    output_dir.mkdir(exist_ok=True)

    search_config = processControl.defaults.get("search", {})
    availables = search_config.get("availables", [])

    if not availables:
        writeLog("warning", logger, "No search sources configured in 'availables'")
        return

    writeLog("info", logger, f"Processing sources: {availables}")

    results = {}
    for source in availables:
        file_pattern = search_config.get(source)
        if not file_pattern:
            writeLog("warning", logger, f"No file pattern defined for source: {source}")
            continue

        data = process_source(source, file_pattern, input_dir, output_dir)
        if data:
            results[source] = data

    total_papers = sum(len(v) for v in results.values())
    writeLog("info", logger, "=" * 60)
    writeLog("info", logger, "NORMALIZATION COMPLETE")
    writeLog("info", logger, "=" * 60)
    for source, data in results.items():
        writeLog("info", logger, f"  {source.upper()}: {len(data)} papers")
    writeLog("info", logger, f"  TOTAL: {total_papers} papers")

    return results


if __name__ == "__main__":
    processNormalizeSearchResults()