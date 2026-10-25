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

from collections import defaultdict
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
from sources.common.streams import load_streams, stream_sources
from sources.ingestion.acl_anthology_provider import AclAnthologyProvider
from sources.ingestion.arxiv_provider import ArxivProvider
from sources.ingestion.ieee_provider import IeeeProvider
from sources.ingestion.openalex_provider import OpenAlexProvider
from sources.ingestion.pubmed_provider import PubmedProvider
from sources.ingestion.scopus_provider import ScopusProvider

PROVIDER_CLASSES = {
    "pubmed": PubmedProvider,
    "scopus": ScopusProvider,
    "ieee": IeeeProvider,
    "openalex": OpenAlexProvider,
    "arxiv": ArxivProvider,
    "acl_anthology": AclAnthologyProvider,
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
    """Deduplica dentro de la fuente por identidad canónica (doi/pmid/título).

    Si un mismo registro aparece en varios flujos, se conserva uno y se
    acumulan sus nombres en `query_streams`.
    """
    by_key: Dict[Any, Dict[str, Any]] = {}
    order: List[Any] = []
    for record in records:
        key = record.get("original_id") or record.get("source_id") or id(record)
        if key in by_key:
            first = by_key[key]
            streams = set(first.get("query_streams") or [])
            streams |= set(record.get("query_streams") or [])
            if record.get("query_stream"):
                streams.add(record["query_stream"])
            if first.get("query_stream"):
                streams.add(first["query_stream"])
            if streams:
                first["query_streams"] = sorted(streams)
            continue
        by_key[key] = record
        order.append(key)
    return [by_key[key] for key in order]


def run_api_ingestion(
    search_config: Dict[str, Any],
    input_dir: Path,
    output_dir: Path,
    strategy_id: Optional[str] = None,
) -> Dict[str, list]:
    """Ejecuta la ingesta para todas las fuentes en modo 'api'.

    Devuelve un dict {source: records} solo para las fuentes que se han
    resuelto por API; el llamador usa modo file para el resto.

    Cuando se pasa ``strategy_id``, se usa la query de esa estrategia
    (conceptsQuery.json > query_strategies.<strategy_id>) y el archivo de
    salida lleva el sufijo de estrategia (ej: arxiv_search_01.json).
    """
    streams = load_streams(search_config)
    all_sources = stream_sources(streams)
    refresh = bool(search_config.get("refresh", False))
    raw_root = Path(input_dir) / "_raw"

    if not any(
        (search_config.get("providers", {}) or {}).get(s, {}).get("mode") == "api"
        for s in all_sources
    ):
        return {}

    client = _build_http_client(search_config)
    resolver = QueryResolver(input_dir, search_config)
    results: Dict[str, list] = {}

    for source in all_sources:
        provider = build_provider(source, search_config, client)
        if provider is None:
            continue

        # Trabajos (flujo, origen, query, max_results) para esta fuente.
        jobs: List[Tuple[str, str, str, int]] = []
        for stream in streams:
            if source not in (stream.get("sources") or []):
                continue
            base_query, query_origin = resolver.for_stream_source(
                stream, source, strategy_id=strategy_id
            )
            if not base_query:
                continue
            if source in ("scopus", "ieee") and query_origin.endswith(":query"):
                writeLog(
                    "warning", logger,
                    f"[{source.upper()}] Usando la query genérica en el flujo "
                    f"'{stream['name']}'; {source} tiene su propia sintaxis.",
                )
            jobs.append((stream["name"], query_origin or "query", base_query, stream["max_results"]))
            if stream.get("decomposition"):
                for label, concept_query in resolver.per_concept_queries(source):
                    jobs.append((stream["name"], f"streams.{stream['name']}.concept:{label}",
                                 concept_query, stream["max_results"]))

        if not jobs:
            writeLog("warning", logger, f"[{source.upper()}] Sin query para API; se usará modo file")
            continue

        try:
            store = SnapshotStore(raw_root, source)
            accumulated: List[Dict[str, Any]] = []
            for qi, (stream_name, origin, query, job_max) in enumerate(jobs):
                writeLog("info", logger,
                         f"[{source.upper()}][{stream_name}] query API ({origin}): {query[:140]}...")
                try:
                    query_records, snapshot_name = _fetch_and_normalize(
                        provider, store, query, refresh, job_max
                    )
                except (MissingCredentials, IngestionError) as exc:
                    if qi == 0:
                        raise  # sin la primera query no hay ingesta: degradar a file
                    writeLog("warning", logger,
                             f"[{source.upper()}] Query '{origin}' fallida: {exc}; se omite.")
                    continue
                for record in query_records:
                    record["query_stream"] = stream_name
                    record["_snapshot_name"] = snapshot_name
                accumulated.extend(query_records)

            # Dedup + tope por flujo, y después dedup cruzado (fusiona flujos).
            by_stream: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for record in accumulated:
                by_stream[record.get("query_stream") or "default"].append(record)

            capped: List[Dict[str, Any]] = []
            for stream in streams:
                chunk = by_stream.get(stream["name"], [])
                if chunk:
                    capped.extend(_dedup_records(chunk)[:stream["max_results"]])

            records = _dedup_records(capped)
            for index, record in enumerate(records, start=1):
                snapshot_name = record.pop("_snapshot_name", "")
                record["source_index"] = index
                record["paper_id"] = f"{source}_{index}"
                record["raw_source_file"] = snapshot_name
                record["snapshot_ref"] = f"_raw/{source}/{snapshot_name}"
                # Etiquetar con la estrategia para trazabilidad en PRISMA
                if strategy_id:
                    record.setdefault("query_strategies", [])
                    if strategy_id not in record["query_strategies"]:
                        record["query_strategies"].append(strategy_id)

            if records:
                stream_counts = defaultdict(int)
                for record in records:
                    for s in (record.get("query_streams") or [record.get("query_stream")]):
                        if s:
                            stream_counts[s] += 1
                strategy_suffix = f"_{strategy_id}" if strategy_id else ""
                output_file = Path(output_dir) / f"{source}_search{strategy_suffix}.json"
                save_json(records, output_file)
                results[f"{source}_{strategy_id}"] = records if strategy_id else results.setdefault(source, records)
                writeLog("info", logger,
                         f"[{source.upper()}] {len(records)} papers guardados en {output_file} "
                         f"({dict(stream_counts)})")
            else:
                writeLog("warning", logger, f"[{source.upper()}] La API no devolvió registros válidos")

        except MissingCredentials as exc:
            writeLog("error", logger, f"[{source.upper()}] Sin credenciales ({exc}); fallback a modo file")
        except IngestionError as exc:
            writeLog("error", logger, f"[{source.upper()}] Fallo de ingesta API ({exc}); fallback a modo file")
        except Exception as exc:  # noqa: BLE001 - se quiere degradar sin abortar el pipeline
            writeLog("exception", logger, f"[{source.upper()}] Error inesperado en ingesta API: {exc}")

    return results
