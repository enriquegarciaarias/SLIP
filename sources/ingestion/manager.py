#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
manager.py
==========

Orquestación de la ingesta API-first.

Responsabilidades:
  - Construir los proveedores de API configurados (`search.providers`).
  - Resolver la query por fuente (`QueryResolver`).
  - Reutilizar o crear el snapshot crudo (`SnapshotStore`).
  - Normalizar offline y escribir `{source}_search.json`.
  - Degradar con elegancia a modo `file` si falta credencial o falla la API.

El resto del pipeline no cambia: sigue consumiendo `{source}_search.json`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sources.common.common import logger, writeLog
from sources.common.dataManager import save_json
from sources.ingestion.base import (
    HttpClient,
    IngestionError,
    MissingCredentials,
    QueryResolver,
    SearchProvider,
    SnapshotStore,
)
from sources.ingestion.ieee_provider import IeeeProvider
from sources.ingestion.openalex_provider import OpenAlexProvider
from sources.ingestion.pubmed_provider import PubmedProvider
from sources.ingestion.scopus_provider import ScopusProvider

PROVIDER_CLASSES = {
    "pubmed": PubmedProvider,
    "scopus": ScopusProvider,
    "ieee": IeeeProvider,
    "openalex": OpenAlexProvider,
}


def _build_http_client(search_config: Dict[str, Any]) -> HttpClient:
    return HttpClient(
        timeout=int(search_config.get("request_timeout", 30)),
        max_retries=int(search_config.get("max_retries", 3)),
        backoff=float(search_config.get("backoff_seconds", 2.0)),
        sleep=float(search_config.get("sleep_between_calls", 0.4)),
    )


def build_provider(
    source: str, search_config: Dict[str, Any], client: HttpClient
) -> Optional[SearchProvider]:
    """Devuelve un proveedor API si la fuente está configurada en modo 'api'."""
    provider_cfg = (search_config.get("providers", {}) or {}).get(source, {}) or {}
    mode = provider_cfg.get("mode", "file")
    if mode != "api":
        return None

    provider_class = PROVIDER_CLASSES.get(source)
    if provider_class is None:
        writeLog("warning", logger, f"[INGEST] Sin proveedor API para '{source}'; se usará modo file")
        return None

    return provider_class(source, provider_cfg, client=client)


def _fetch_and_normalize(
    provider: SearchProvider,
    store: SnapshotStore,
    query: str,
    refresh: bool,
    max_results: int,
) -> Tuple[List[Dict[str, Any]], str]:
    """Recupera (reutilizando snapshot si procede) y normaliza una query."""
    snapshot = None if refresh else store.latest(query)
    if snapshot is not None:
        payload = store.load(snapshot)
        raw = payload.get("raw", {})
        writeLog("info", logger, f"[{provider.source.upper()}] Reutilizando snapshot {snapshot.name}")
    else:
        raw = provider.fetch(query, max_results=max_results)
        snapshot = store.save(query, raw, extra={"provider": provider.source})
    records = provider.normalize(raw) or []
    return records, snapshot.name


def _dedup_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplica dentro de la fuente por identidad canónica (doi/pmid/título)."""
    seen = set()
    unique: List[Dict[str, Any]] = []
    for record in records:
        key = record.get("original_id") or record.get("source_id") or id(record)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def run_api_ingestion(
    search_config: Dict[str, Any],
    input_dir: Path,
    output_dir: Path,
) -> Dict[str, list]:
    """Ejecuta la ingesta para todas las fuentes en modo 'api'.

    Devuelve un dict {source: records} solo para las fuentes que se han
    resuelto por API; el llamador usa modo file para el resto.
    """
    availables = search_config.get("availables", [])
    refresh = bool(search_config.get("refresh", False))
    max_results = int(search_config.get("max_results", 500))
    raw_root = Path(input_dir) / "_raw"

    if not any(
        (search_config.get("providers", {}) or {}).get(s, {}).get("mode") == "api"
        for s in availables
    ):
        return {}

    # Query decomposition: además de la query global de la fuente, lanzar una
    # consulta por pregunta de investigación (conceptsQuery.json > concepts[]).
    decomp_cfg = search_config.get("decomposition", {}) or {}
    decomposition_enabled = bool(decomp_cfg.get("enabled", False))
    decomp_sources = decomp_cfg.get("sources") or availables

    client = _build_http_client(search_config)
    resolver = QueryResolver(input_dir, search_config)
    results: Dict[str, list] = {}

    for source in availables:
        provider = build_provider(source, search_config, client)
        if provider is None:
            continue

        try:
            base_query, query_origin = resolver.for_source(source)
            if not base_query:
                writeLog("warning", logger, f"[{source.upper()}] Sin query para API; se usará modo file")
                continue

            if source in ("scopus", "ieee") and query_origin.endswith(":query"):
                writeLog(
                    "warning", logger,
                    f"[{source.upper()}] Usando la query genérica; {source} tiene su "
                    f"propia sintaxis. Define 'queries.{source}' en conceptsQuery.json.",
                )

            # Lista de queries: global + (si procede) una por RQ.
            queries: List[Tuple[str, str]] = [(query_origin or "query", base_query)]
            if decomposition_enabled and source in decomp_sources:
                for label, concept_query in resolver.per_concept_queries(source):
                    queries.append((f"concept:{label}", concept_query))
                if len(queries) > 1:
                    writeLog("info", logger,
                             f"[{source.upper()}] Decomposition activa: {len(queries)} queries "
                             f"(1 global + {len(queries) - 1} por RQ)")

            store = SnapshotStore(raw_root, source)
            accumulated: List[Dict[str, Any]] = []
            for qi, (origin, query) in enumerate(queries):
                writeLog("info", logger, f"[{source.upper()}] query API ({origin}): {query[:160]}...")
                try:
                    query_records, snapshot_name = _fetch_and_normalize(
                        provider, store, query, refresh, max_results
                    )
                except (MissingCredentials, IngestionError) as exc:
                    if qi == 0:
                        raise  # sin la query global no hay ingesta: degradar a file
                    writeLog("warning", logger,
                             f"[{source.upper()}] Query por RQ fallida ({origin}): {exc}; se omite.")
                    continue
                for record in query_records:
                    record["_snapshot_name"] = snapshot_name
                accumulated.extend(query_records)

            records = _dedup_records(accumulated)[:max_results]
            for index, record in enumerate(records, start=1):
                snapshot_name = record.pop("_snapshot_name", "")
                record["source_index"] = index
                record["paper_id"] = f"{source}_{index}"
                record["raw_source_file"] = snapshot_name
                record["snapshot_ref"] = f"_raw/{source}/{snapshot_name}"

            if records:
                if len(queries) > 1:
                    writeLog("info", logger,
                             f"[{source.upper()}] Decomposition: {len(accumulated)} crudos -> "
                             f"{len(records)} únicos")
                output_file = Path(output_dir) / f"{source}_search.json"
                save_json(records, output_file)
                results[source] = records
                writeLog("info", logger, f"[{source.upper()}] {len(records)} papers guardados en {output_file}")
            else:
                writeLog("warning", logger, f"[{source.upper()}] La API no devolvió registros válidos")

        except MissingCredentials as exc:
            writeLog("error", logger, f"[{source.upper()}] Sin credenciales ({exc}); fallback a modo file")
        except IngestionError as exc:
            writeLog("error", logger, f"[{source.upper()}] Fallo de ingesta API ({exc}); fallback a modo file")
        except Exception as exc:  # noqa: BLE001 - se quiere degradar sin abortar el pipeline
            writeLog("exception", logger, f"[{source.upper()}] Error inesperado en ingesta API: {exc}")

    return results
