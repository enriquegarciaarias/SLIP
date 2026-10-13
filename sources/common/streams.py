#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
streams.py
==========

Abstracción de "flujos de búsqueda" (search streams).

Un flujo es un grupo de búsqueda con nombre propio: sus propias queries, sus
propias fuentes, su propio `max_results` y su propio criterio de ranking. El
pipeline puede tener N flujos; cada paper queda etiquetado con el/los flujo(s)
que lo encontraron (`query_streams`), lo que permite rankings y elegibilidad
independientes (p. ej. un flujo "physio" anclado al dominio y un flujo "nlp"
metodológico).

Retrocompatibilidad: si `config.json > search.streams` no existe o está vacío,
se sintetiza un único flujo `"default"` equivalente al comportamiento histórico
(`availables` + query global de `conceptsQuery.json`).
"""

from __future__ import annotations

from typing import Any, Dict, List

DEFAULT_STREAM = "default"


def load_streams(search_config: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    """Normaliza la config de flujos. Devuelve siempre una lista no vacía."""
    search_config = search_config or {}
    raw = search_config.get("streams")

    if isinstance(raw, dict) and raw:
        streams: List[Dict[str, Any]] = []
        for name, cfg in raw.items():
            cfg = cfg or {}
            if not cfg.get("enabled", True):
                continue
            streams.append({
                "name": str(name),
                "query": (cfg.get("query") or "").strip(),
                "queries": dict(cfg.get("queries") or {}),
                "sources": list(cfg.get("sources") or search_config.get("availables", [])),
                "max_results": int(cfg.get("max_results", search_config.get("max_results", 500))),
                "ranking_query": (cfg.get("ranking_query") or "").strip(),
                "top_k": cfg.get("top_k"),
                "decomposition": bool(cfg.get("decomposition", False)),
            })
        if streams:
            return streams

    # Flujo único legacy.
    decomposition = search_config.get("decomposition") or {}
    return [{
        "name": DEFAULT_STREAM,
        "query": "",
        "queries": {},
        "sources": list(search_config.get("availables", [])),
        "max_results": int(search_config.get("max_results", 500)),
        "ranking_query": "",
        "top_k": None,
        "decomposition": bool(decomposition.get("enabled", False)),
        "_legacy": True,
    }]


def stream_sources(streams: List[Dict[str, Any]]) -> List[str]:
    """Unión ordenada de fuentes de todos los flujos."""
    ordered: List[str] = []
    for stream in streams or []:
        for source in stream.get("sources", []) or []:
            if source not in ordered:
                ordered.append(source)
    return ordered


def stream_names_for_source(streams: List[Dict[str, Any]], source: str) -> List[str]:
    """Nombres de los flujos que incluyen `source`."""
    return [s["name"] for s in streams or [] if source in (s.get("sources") or [])]
