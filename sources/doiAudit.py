#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
doiAudit.py
===========

Verificación de DOIs contra OpenAlex.

Para cada registro con DOI, resuelve el DOI en OpenAlex y compara el título
devuelto con el título del registro. Detecta:
  - ``mismatch`` : el DOI existe pero apunta a otro artículo.
  - ``unknown``  : el DOI no resuelve en OpenAlex.

Pensado para auditar ``canonical.json`` (o cualquier ``*_search.json``) antes
de citar. No modifica nada.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

from sources.common.common import logger, writeLog
from sources.common.utils import normalize_text
from sources.ingestion.base import HttpClient

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
DEFAULT_THRESHOLD = 0.6


def _norm_title(text: str) -> set:
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return {t for t in text.split() if len(t) > 1}


def title_similarity(a: str, b: str) -> float:
    """Coeficiente de solapamiento de tokens (robusto a subtítulos/puntuación)."""
    ta, tb = _norm_title(a), _norm_title(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _clean_doi(doi: str) -> str:
    doi = (doi or "").strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi.strip()


def fetch_openalex_titles(
    dois: List[str], client: HttpClient, batch_size: int = 50, mailto: str = ""
) -> Dict[str, Dict[str, Any]]:
    """Devuelve {doi_normalizado: {title, year}} para los DOIs hallados."""
    found: Dict[str, Dict[str, Any]] = {}
    clean = sorted({_clean_doi(d) for d in dois if _clean_doi(d)})
    for start in range(0, len(clean), batch_size):
        chunk = clean[start:start + batch_size]
        params: Dict[str, Any] = {
            "filter": "doi:" + "|".join(chunk),
            "per-page": len(chunk),
            "select": "doi,title,publication_year",
        }
        if mailto:
            params["mailto"] = mailto
        try:
            data = client.get(OPENALEX_WORKS_URL, params=params)
        except Exception as exc:  # noqa: BLE001
            writeLog("warning", logger, f"[DoiAudit] Falló lote OpenAlex: {exc}")
            continue
        for work in (data or {}).get("results", []) or []:
            doi = _clean_doi(work.get("doi", ""))
            if doi:
                found[doi] = {
                    "title": work.get("title") or "",
                    "year": work.get("publication_year"),
                }
    return found


def audit_dois(
    papers: List[dict],
    client: Optional[HttpClient] = None,
    threshold: float = DEFAULT_THRESHOLD,
    mailto: str = "",
) -> Dict[str, Any]:
    """Audita los DOIs de una lista de registros. Devuelve un informe."""
    client = client or HttpClient(timeout=30, max_retries=3, backoff=2.0, sleep=0.2)

    indexed = {}
    for paper in papers:
        doi = _clean_doi(paper.get("doi", ""))
        if doi:
            indexed.setdefault(doi, []).append(paper)

    resolved = fetch_openalex_titles(list(indexed), client, mailto=mailto)

    findings: List[Dict[str, Any]] = []
    n_with_doi = 0
    n_ok = 0
    for doi, records in indexed.items():
        n_with_doi += len(records)
        work = resolved.get(doi)
        for record in records:
            if work is None:
                findings.append({
                    "kind": "unknown",
                    "doi": doi,
                    "paper_id": record.get("paper_id"),
                    "title": record.get("title", ""),
                    "openalex_title": None,
                    "similarity": None,
                })
                continue
            sim = title_similarity(record.get("title", ""), work["title"])
            if sim < threshold:
                findings.append({
                    "kind": "mismatch",
                    "doi": doi,
                    "paper_id": record.get("paper_id"),
                    "title": record.get("title", ""),
                    "openalex_title": work["title"],
                    "openalex_year": work.get("year"),
                    "similarity": round(sim, 3),
                })
            else:
                n_ok += 1

    report = {
        "n_papers": len(papers),
        "n_with_doi": n_with_doi,
        "n_ok": n_ok,
        "n_unknown": sum(1 for f in findings if f["kind"] == "unknown"),
        "n_mismatch": sum(1 for f in findings if f["kind"] == "mismatch"),
        "findings": findings,
    }
    return report


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Auditoría de DOIs contra OpenAlex")
    parser.add_argument("path", help="canonical.json o *_search.json")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--mailto", default="")
    parser.add_argument("--json", help="ruta para volcar el informe JSON")
    args = parser.parse_args()

    data = json.load(open(args.path, encoding="utf-8"))
    papers = data if isinstance(data, list) else data.get("papers", [])
    report = audit_dois(papers, threshold=args.threshold, mailto=args.mailto)

    print(f"Papers: {report['n_papers']} | con DOI: {report['n_with_doi']} | "
          f"OK: {report['n_ok']} | mismatch: {report['n_mismatch']} | "
          f"sin resolver: {report['n_unknown']}")
    for f in report["findings"][:30]:
        if f["kind"] == "mismatch":
            print(f"[MISMATCH] {f['paper_id']} doi={f['doi']} sim={f['similarity']}")
            print(f"    registro: {f['title'][:90]}")
            print(f"    OpenAlex: {f['openalex_title'][:90]}")
        else:
            print(f"[UNKNOWN ] {f['paper_id']} doi={f['doi']} | {f['title'][:80]}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"Informe guardado en {args.json}")


if __name__ == "__main__":
    main()
