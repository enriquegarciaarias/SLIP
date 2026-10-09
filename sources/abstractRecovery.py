#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
abstractRecovery.py
===================

Recuperación de abstracts completos para fuentes cuyos exports los traen
truncados (ACM DL y arXiv).

Estrategia:
  1. OpenAlex por DOI (vía principal, fiable y sin cuotas agresivas):
       - ACM   : DOI tal cual (`10.1145/...`).
       - arXiv : DOI derivado `10.48550/arXiv.<id>`.
  2. API Atom de arXiv (fallback opcional si OpenAlex no tiene el abstract).

El módulo es idempotente: solo toca registros marcados por
`needs_abstract_recovery`. Si la recuperación falla, el registro se conserva
tal cual (nunca aborta la normalización).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional

from sources.common.common import logger, writeLog
from sources.common.utils import normalize_text, needs_abstract_recovery
from sources.ingestion.base import HttpClient
from sources.ingestion.openalex_provider import _reconstruct_abstract

ARXIV_API_URL = "https://export.arxiv.org/api/query"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
ARXIV_DOI_PREFIX = "10.48550/arXiv."
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

_VERSION_RE = re.compile(r"v\d+$", re.IGNORECASE)


def _normalize_arxiv_id(value: str) -> str:
    """Normaliza un id de arXiv quitando URL/versión: `2610.02866v1` -> `2610.02866`."""
    if not value:
        return ""
    value = value.strip().rsplit("/abs/", 1)[-1]
    value = value.replace("arXiv:", "").strip()
    return _VERSION_RE.sub("", value)


def _build_client(search_config: Optional[Dict[str, Any]], **overrides: Any) -> HttpClient:
    search_config = search_config or {}
    return HttpClient(
        timeout=int(overrides.get("request_timeout", search_config.get("request_timeout", 30))),
        max_retries=int(overrides.get("max_retries", search_config.get("max_retries", 3))),
        backoff=float(overrides.get("backoff_seconds", search_config.get("backoff_seconds", 2.0))),
        sleep=float(overrides.get("sleep_between_calls", search_config.get("sleep_between_calls", 0.4))),
    )


# ==========================================================================
# OpenAlex (vía principal)
# ==========================================================================

def fetch_openalex_abstract(doi: str, client: HttpClient, mailto: str = "") -> str:
    """Reconstruye el abstract de un DOI vía OpenAlex (vacío si no existe)."""
    if not doi:
        return ""
    params: Dict[str, Any] = {"select": "abstract_inverted_index"}
    if mailto:
        params["mailto"] = mailto
    try:
        data = client.get(f"{OPENALEX_WORKS_URL}/https://doi.org/{doi}", params=params)
    except Exception:  # noqa: BLE001 - 404 u otros: el registro se queda sin abstract
        return ""
    return _reconstruct_abstract((data or {}).get("abstract_inverted_index"))


def _recover_via_openalex(
    records: List[dict],
    client: HttpClient,
    doi_getter: Callable[[dict], str],
    mailto: str = "",
) -> int:
    recovered = 0
    for record in records:
        if not needs_abstract_recovery(record.get("abstract")):
            continue
        doi = doi_getter(record) or ""
        full = fetch_openalex_abstract(doi, client, mailto)
        if full:
            record["abstract"] = full
            record["abstract_truncated"] = False
            record["abstract_recovered"] = True
            record["abstract_source"] = "openalex"
            recovered += 1
    return recovered


# ==========================================================================
# arXiv API (fallback)
# ==========================================================================

def fetch_arxiv_abstracts(ids: List[str], client: HttpClient, batch_size: int = 50) -> Dict[str, str]:
    """Devuelve {arxiv_id: abstract} consultando la API de arXiv por lotes."""
    abstracts: Dict[str, str] = {}
    clean_ids = [i for i in (_normalize_arxiv_id(x) for x in ids) if i]

    for start in range(0, len(clean_ids), batch_size):
        chunk = clean_ids[start:start + batch_size]
        params = {"id_list": ",".join(chunk), "max_results": len(chunk)}
        try:
            text = client.get(ARXIV_API_URL, params=params, accept_json=False)
        except Exception as exc:  # noqa: BLE001 - degradar sin abortar
            writeLog("warning", logger, f"[arXiv] Falló la consulta de abstracts ({exc})")
            continue

        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            writeLog("warning", logger, f"[arXiv] Respuesta no parseable ({exc})")
            continue

        for entry in root.findall("a:entry", ATOM_NS):
            entry_id = entry.findtext("a:id", "", ATOM_NS)
            summary = entry.findtext("a:summary", "", ATOM_NS)
            aid = _normalize_arxiv_id(entry_id)
            if aid and summary:
                abstracts[aid] = normalize_text(summary)

    return abstracts


def _recover_arxiv_api(records: List[dict], client: HttpClient, batch_size: int = 50) -> int:
    pending = [r for r in records if needs_abstract_recovery(r.get("abstract"))]
    ids = [r.get("arxiv_id") for r in pending if r.get("arxiv_id")]
    if not ids:
        return 0

    abstracts = fetch_arxiv_abstracts(ids, client, batch_size=batch_size)
    recovered = 0
    for record in pending:
        abstract = abstracts.get(record.get("arxiv_id"))
        if abstract:
            record["abstract"] = abstract
            record["abstract_truncated"] = False
            record["abstract_recovered"] = True
            record["abstract_source"] = "arxiv"
            recovered += 1
    return recovered


# ==========================================================================
# API pública
# ==========================================================================

def _arxiv_doi(record: dict) -> str:
    aid = record.get("arxiv_id")
    return f"{ARXIV_DOI_PREFIX}{aid}" if aid else (record.get("doi") or "")


def recover_abstracts(
    records: List[dict],
    source: str,
    search_config: Optional[Dict[str, Any]] = None,
    client: Optional[HttpClient] = None,
) -> int:
    """Recupera los abstracts truncados de `records` (in-place). Devuelve cuántos."""
    search_config = search_config or {}
    recovery_cfg = search_config.get("abstract_recovery", {}) or {}
    if not recovery_cfg.get("enabled", True):
        writeLog("info", logger, f"[{source.upper()}] Recuperación de abstracts desactivada")
        return 0

    if not any(needs_abstract_recovery(r.get("abstract")) for r in records):
        return 0

    provider_cfg = (search_config.get("providers", {}) or {}).get(source, {}) or {}
    mailto = (provider_cfg.get("email") or search_config.get("email") or "").strip()

    # Vía principal: OpenAlex.
    client = client or _build_client(search_config)
    if source == "acm":
        recovered = _recover_via_openalex(records, client, lambda r: r.get("doi", ""), mailto)
    elif source == "arxiv":
        recovered = _recover_via_openalex(records, client, _arxiv_doi, mailto)
    else:
        writeLog("warning", logger, f"[{source.upper()}] Sin estrategia de recuperación de abstracts")
        return 0

    # Fallback: API de arXiv para lo que OpenAlex no tenga.
    if source == "arxiv" and recovery_cfg.get("arxiv_fallback", True):
        if any(needs_abstract_recovery(r.get("abstract")) for r in records):
            arxiv_client = _build_client(search_config, backoff_seconds=10.0, max_retries=2)
            recovered += _recover_arxiv_api(
                records, arxiv_client,
                batch_size=int(recovery_cfg.get("arxiv_batch_size", 50)),
            )

    pending = sum(1 for r in records if needs_abstract_recovery(r.get("abstract")))
    writeLog("info", logger,
             f"[{source.upper()}] Abstracts recuperados: {recovered} "
             f"(quedan {pending} sin completar de {len(records)})")
    return recovered
