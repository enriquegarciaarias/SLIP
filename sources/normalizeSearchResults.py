#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
normalizeSearchResults.py

Input:
    input/wos_export.txt (WoS)           → admite múltiples archivos: wos_export*.txt
    input/scopus_export.csv (Scopus)     → admite múltiples archivos: scopus_export*.csv
    input/ieee_export.csv (IEEE Xplore)  → admite múltiples archivos: ieee_export*.csv
    input/pubmed_export.txt (PubMed)     → admite múltiples archivos: pubmed_export*.txt
    ...

Output:
    output/wos_search.json
    output/scopus_search.json
    output/ieee_search.json
    output/pubmed_search.json
    ...

Cada paper recibe un ID único por fuente: {source}_{n}
donde n es un entero incremental (1, 2, 3...) a través de TODOS los archivos de la misma fuente.
"""

from sources.common.common import logger, processControl, writeLog
from sources.common.dataManager import save_json
from sources.common.utils import safe_int, normalized_title, sha1, normalize_text, normalize_doi, inicioModulo

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


# --------------------------------------------------------
# WoS parser — REPLACEMENT for the existing parse_wos_file
# in normalizeSearchResults.py
# --------------------------------------------------------
#
# CHANGED: the original implementation used
#     csv.DictReader(f, delimiter="\t")
# which assumes WoS exports as tab-separated values with a header row.
# The real export format is WoS "Tagged Field Format" — structurally
# the same style already handled correctly by parse_pubmed_file in this
# module: a two-letter tag, a value, optional indented continuation
# lines, fields separated by blank lines... except WoS additionally
# terminates each record with a line "ER" (PubMed has no such marker).
#
# With the old TSV-based parser, no column ever matched "TI", "AB",
# "DI", etc. — every WoS record silently produced title=None,
# abstract=None, and so on. This was confirmed against a real export
# sample and against canonical.json, where wos_1 had every text field
# as null. The fix below mirrors parse_pubmed_file's tag-parsing logic,
# adapted to WoS's tag set and its ER record terminator.
#
# Field mapping used (WoS tag -> output field):
#   TI -> title            AB -> abstract         DI -> doi
#   AU -> authors (one per AU/continuation line)
#   DE -> keywords (author keywords, ';' or newline separated)
#   ID -> keywords_plus (WoS "KeyWords Plus", appended if present)
#   PY -> year              SO -> venue            DT -> document_type
#   LA -> language          NR -> references_count TC -> citation_count
#   UT -> accession number (WoS unique identifier, e.g. "WOS:001115856700009")
#   PM -> pmid (when present, some WoS records carry a PubMed ID)
# --------------------------------------------------------

def parse_wos_file(filepath, source_name="wos", start_index=1):
    """
    Parses a WoS export in Tagged Field Format and assigns incremental
    paper_ids. start_index allows concatenating multiple files for the
    same source.

    Format reference (matches the sample export):
        FN Clarivate Analytics Web of Science
        VR 1.0
        PT J
        AU Sajno, Elena
           Rossi, Alessio
        TI XAI in Affective Computing: a Preliminary Study
        SO ANNUAL REVIEW OF CYBERTHERAPY AND TELEMEDICINE
        AB Affective computing is a rapidly growing field...
        TC 1
        PY 2023
        UT WOS:001115856700009
        ER

        PT J
        AU ...
        ER
    """
    papers = []
    idx = start_index
    skipped_count = 0

    with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as f:
        content = f.read()

    # Records are separated by a line containing only "ER" (End of Record).
    # CHANGED: split on ER rather than blank lines — WoS records can
    # contain blank-line-free continuation blocks, and relying on ER as
    # the explicit terminator (as the format itself defines) is more
    # robust than inferring boundaries from whitespace.
    raw_records = re.split(r'\nER\s*\n', content)

    for record in raw_records:
        record = record.strip()
        if not record or record.startswith("FN ") or record.startswith("VR "):
            # Skip the file header block (FN/VR lines) if it ends up
            # alone in a split segment (e.g. before the first PT line).
            if not re.search(r'^\s*(AU|TI|AB)\s', record, re.MULTILINE):
                continue

        # --------------------------------------------------
        # Tag parsing: a tag is 2-4 uppercase letters at the start of a
        # line, followed by whitespace and the value. Continuation lines
        # are indented (typically 3 spaces) and belong to the previous tag.
        # Same approach as parse_pubmed_file's tokenizer.
        # --------------------------------------------------
        fields: dict[str, list[str]] = {}
        current_key = None

        for line in record.split('\n'):
            if not line.strip():
                continue

            match = re.match(r'^([A-Z]{2,4})\s(.*)$', line)
            # A continuation line is indented and does NOT match the
            # tag pattern (WoS continuations start with spaces, e.g.
            # "   Rossi, Alessio" under AU).
            if match and not line.startswith(" "):
                current_key = match.group(1)
                fields.setdefault(current_key, []).append(match.group(2).strip())
            else:
                if current_key:
                    fields[current_key].append(line.strip())

        if not fields:
            continue

        # --------------------------------------------------
        # Extract fields
        # --------------------------------------------------

        title = normalized_title(" ".join(fields.get("TI", [])))
        abstract = normalize_text(" ".join(fields.get("AB", [])))
        doi = normalize_doi(" ".join(fields.get("DI", [])))

        # AU has one author per list entry (tag line + each continuation
        # line is a separate author in WoS, unlike AB/TI where
        # continuations are wrapped text of the same field).
        authors = [a.strip() for a in fields.get("AU", []) if a.strip()]

        # DE = author keywords; ID = WoS "KeyWords Plus" (index terms).
        # Both are ';' separated within their own lines, but may also
        # span multiple continuation lines — join then split on ';'.
        keywords = []
        de_raw = " ".join(fields.get("DE", []))
        if de_raw:
            keywords.extend([k.strip() for k in de_raw.split(";") if k.strip()])
        id_raw = " ".join(fields.get("ID", []))
        if id_raw:
            keywords.extend([k.strip() for k in id_raw.split(";") if k.strip()])
        keywords = sorted(set(keywords))

        try:
            year = int(" ".join(fields.get("PY", [])).strip())
        except (ValueError, TypeError):
            year = None

        venue = normalize_text(" ".join(fields.get("SO", [])))
        document_type = normalize_text(" ".join(fields.get("DT", [])))
        language = normalize_text(" ".join(fields.get("LA", [])))

        references_count = safe_int(" ".join(fields.get("NR", [])))
        citation_count = safe_int(" ".join(fields.get("TC", [])))

        accession_number = " ".join(fields.get("UT", [])).strip()  # e.g. "WOS:001115856700009"
        pmid = " ".join(fields.get("PM", [])).strip()

        # --------------------------------------------------
        # Validation — mirrors parse_pubmed_file's quality gate.
        # Records with no usable title/abstract are skipped rather than
        # silently producing an empty downstream record (the original
        # bug's actual symptom).
        # --------------------------------------------------
        if not title or len(title) < 5:
            skipped_count += 1
            idx += 1
            continue

        landing_page = None
        if doi:
            landing_page = f"https://doi.org/{doi}"
        elif accession_number:
            landing_page = f"https://www.webofscience.com/wos/woscc/full-record/{accession_number}"

        paper = {
            "paper_id": build_source_paper_id(source_name, idx),
            "source": source_name,
            "source_index": idx,
            "original_id": f"doi:{doi.lower().strip()}" if doi else f"title:{sha1(title)}",
            "doi": doi,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "keywords": keywords,
            "year": year,
            "venue": venue,
            "document_type": document_type,
            "language": language,
            "references_count": references_count,
            "citation_count": citation_count,
            "relevance_score": None,
            "selected": False,
            "has_full_text": False,
            "raw_source_file": os.path.basename(filepath),
            "pdf_url": None,
            "landing_page": landing_page,
            "accession_number": accession_number if accession_number else None,
            "pmid": pmid if pmid else None,
        }

        papers.append(paper)
        idx += 1

    if skipped_count > 0:
        writeLog("info", logger,
                 f"[WoS] Skipped {skipped_count} record(s) with missing/short title "
                 f"in {os.path.basename(filepath)}")

    writeLog("info", logger,
             f"[WoS] Parsed {len(papers)} record(s) from {os.path.basename(filepath)}")

    return papers


# ==========================================================
# Scopus parser (con start_index)
# ==========================================================

def parse_scopus_file(filepath, source_name="scopus", start_index=1):
    """
    Parsea archivo Scopus y asigna IDs incrementales.
    start_index: índice inicial para esta fuente.
    """
    papers = []
    idx = start_index

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
                "paper_id": build_source_paper_id(source_name, idx),
                "source": source_name,
                "source_index": idx,
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
            idx += 1

    return papers


# ==========================================================
# IEEE Xplore parser (con start_index)
# ==========================================================

def parse_ieee_file(filepath, source_name="ieee", start_index=1):
    """
    Parsea archivo IEEE Xplore CSV y asigna IDs incrementales.
    start_index: índice inicial para esta fuente.
    """
    papers = []
    idx = start_index

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
                "paper_id": build_source_paper_id(source_name, idx),
                "source": source_name,
                "source_index": idx,
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
            idx += 1

    return papers


# ==========================================================
# PubMed parser (con start_index) - NUEVO
# ==========================================================

def parse_pubmed_file(filepath, source_name="pubmed", start_index=1):
    """
    Parsea archivo PubMed en formato estándar con etiquetas (PMID-, TI -, AB -, etc.)
    start_index: índice inicial para esta fuente.
    """
    papers = []
    idx = start_index
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
        title = normalized_title(fields.get('TI', ''))
        abstract = normalize_text(fields.get('AB', ''))
        doi = normalize_doi(fields.get('LID', '').split(' ')[0] if fields.get('LID') else '')
        pmid = fields.get('PMID', '')
        pmcid = fields.get('PMC', '')
        year = None
        dp = fields.get('DP', '')
        if dp:
            year_match = re.search(r'(\d{4})', dp)
            if year_match:
                year = int(year_match.group(1))

        # Autores
        authors = []
        for line in record.split('\n'):
            match = re.match(r'^(FAU|AU)\s*[-–]\s*(.*)$', line)
            if match:
                author = match.group(2).strip()
                if author:
                    authors.append(author)

        # Keywords (MESH)
        keywords = []
        for line in record.split('\n'):
            match = re.match(r'^MH\s*[-–]\s*(.*)$', line)
            if match:
                term = match.group(1).strip()
                if term and term not in keywords:
                    keywords.append(term)

        venue = fields.get('TA', fields.get('JT', ''))

        # Validación
        if not title or len(title) < 10:
            skipped_count += 1
            idx += 1
            continue

        if not abstract or len(abstract) < 50:
            skipped_count += 1
            idx += 1
            continue

        landing_page = None
        if doi:
            landing_page = f"https://doi.org/{doi}"
        elif pmid:
            landing_page = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

        paper = {
            "paper_id": build_source_paper_id(source_name, idx),
            "source": source_name,
            "source_index": idx,
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
            "source_fields": fields
        }

        papers.append(paper)
        idx += 1

    if skipped_count > 0:
        writeLog("info", logger, f"[PubMed] Skipped {skipped_count} incomplete records in {os.path.basename(filepath)}")
    return papers


# ==========================================================
# Parser genérico (para futuras fuentes)
# ==========================================================

def parse_generic_file(filepath, source_name, parser_type="csv"):
    writeLog("warning", logger, f"Parser genérico para {source_name} no implementado")
    return []


# ==========================================================
# Función principal unificada (AHORA SOPORTA MÚLTIPLES ARCHIVOS)
# ==========================================================

def process_source(known_source: str, file_pattern: str, input_dir: Path, output_dir: Path):
    """
    Procesa una fuente de datos según su tipo.
    AHORA SOPORTA MÚLTIPLES ARCHIVOS que coincidan con file_pattern.
    Los IDs se asignan de forma incremental a través de todos los archivos.
    """
    source_files = sorted(glob.glob(str(input_dir / file_pattern)))

    if not source_files:
        writeLog("warning", logger, f"[{known_source.upper()}] No files found matching: {file_pattern}")
        return None

    all_data = []
    global_index = 1

    for filepath in source_files:
        writeLog("info", logger, f"[{known_source.upper()}] Processing: {filepath}")

        if known_source == "wos":
            data = parse_wos_file(filepath, known_source, start_index=global_index)
        elif known_source == "scopus":
            data = parse_scopus_file(filepath, known_source, start_index=global_index)
        elif known_source == "ieee":
            data = parse_ieee_file(filepath, known_source, start_index=global_index)
        elif known_source == "pubmed":
            data = parse_pubmed_file(filepath, known_source, start_index=global_index)
        else:
            data = parse_generic_file(filepath, known_source)

        if data:
            # Actualizar el índice global para el siguiente archivo
            global_index += len(data)
            all_data.extend(data)

    if all_data:
        output_file = output_dir / f"{known_source}_search.json"
        save_json(all_data, output_file)
        writeLog("info", logger, f"[{known_source.upper()}] {len(all_data)} papers saved to {output_file}")
        return all_data

    return None


# ==========================================================
# Main
# ==========================================================

def processNormalizeSearchResults():
    """
    Procesa todas las fuentes definidas en processControl.defaults.get("search")
    """
    input_dir, output_dir = inicioModulo("processNormalizeSearchResults")

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