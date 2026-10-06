#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
base.py
=======

Infraestructura común de la capa de ingesta API-first de SLIP.

Contiene:
  - HttpClient        : cliente HTTP con reintentos/backoff y pausas.
  - SnapshotStore     : guarda/recupera las respuestas crudas de cada API.
  - QueryResolver     : resuelve la query por fuente (conceptsQuery.json).
  - SearchProvider    : interfaz (ABC) de un proveedor de búsqueda.
  - CanonicalBuilder  : construye el registro canónico común a todas las fuentes.

El registro canónico conserva la forma que ya consumen `searchMergeEngine` y
el resto del pipeline, y añade campos nuevos (pmid, references, oa_url, ...).
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from sources.common.common import logger, writeLog
from sources.common.utils import normalize_text, normalize_doi, normalized_title, sha1


# ==========================================================================
# Excepciones
# ==========================================================================

class IngestionError(Exception):
    """Error genérico de la capa de ingesta."""


class MissingCredentials(IngestionError):
    """Falta una API key o entitlement necesario para una fuente."""


# ==========================================================================
# Cliente HTTP
# ==========================================================================

class HttpClient:
    """Cliente HTTP síncrono con reintentos exponenciales y pausa entre llamadas.

    Pensado para APIs bibliográficas con cuotas y rate-limits. Reintenta ante
    429 y 5xx; ante 401/403 lanza `MissingCredentials` (no tiene sentido
    reintentar sin cambiar la clave).
    """

    _RETRYABLE_STATUS = (429, 500, 502, 503, 504)

    def __init__(
        self,
        timeout: int = 30,
        max_retries: int = 3,
        backoff: float = 2.0,
        sleep: float = 0.4,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.sleep = sleep
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "SLIP/1.0 (Scientific Literature Intelligence Pipeline)"}
        )

    def get(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        accept_json: bool = True,
    ) -> Any:
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )

                if response.status_code in (401, 403):
                    raise MissingCredentials(
                        f"HTTP {response.status_code} en {url}: revisa API key/entitlement"
                    )

                if response.status_code in self._RETRYABLE_STATUS:
                    wait = self.backoff * (attempt + 1)
                    writeLog(
                        "warning", logger,
                        f"[HTTP] {response.status_code} en {url}; reintento en {wait:.1f}s",
                    )
                    time.sleep(wait)
                    continue

                response.raise_for_status()

                if self.sleep:
                    time.sleep(self.sleep)

                return response.json() if accept_json else response.text

            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                wait = self.backoff * (attempt + 1)
                writeLog("warning", logger, f"[HTTP] error {exc}; reintento en {wait:.1f}s")
                time.sleep(wait)

        raise IngestionError(f"GET falló para {url}: {last_error}")


# ==========================================================================
# SnapshotStore
# ==========================================================================

class SnapshotStore:
    """Persistencia de la respuesta cruda de una API.

    Estructura en disco:
        {raw_root}/{source}/{source}_{query_hash}_{timestamp}.json
        {raw_root}/fetch_manifest.json

    Cada snapshot encapsula `meta` (query, fecha, hash) y `raw` (lo devuelto
    por el proveedor), de modo que `normalize()` puede re-ejecutarse offline.
    """

    def __init__(self, raw_root: str | Path, source: str) -> None:
        self.source = source
        self.root = Path(raw_root)
        self.dir = self.root / source
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "fetch_manifest.json"

    @staticmethod
    def query_hash(query: str) -> str:
        return sha1(query or "")[:12]

    def latest(self, query: str) -> Optional[Path]:
        """Devuelve el snapshot más reciente para esa query, si existe."""
        qh = self.query_hash(query)
        files = sorted(self.dir.glob(f"{self.source}_{qh}_*.json"))
        return files[-1] if files else None

    def save(self, query: str, raw: Any, extra: Optional[Dict[str, Any]] = None) -> Path:
        qh = self.query_hash(query)
        timestamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        path = self.dir / f"{self.source}_{qh}_{timestamp}.json"

        meta = {
            "source": self.source,
            "query": query,
            "query_hash": qh,
            "fetched_at": timestamp,
            "extra": extra or {},
        }
        payload = {"meta": meta, "raw": raw}

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

        self._append_manifest(meta, path)
        writeLog("info", logger, f"[SNAPSHOT] {self.source}: guardado {path}")
        return path

    def load(self, path: str | Path) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _append_manifest(self, meta: Dict[str, Any], path: Path) -> None:
        manifest: List[Dict[str, Any]] = []
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as handle:
                    manifest = json.load(handle)
            except (json.JSONDecodeError, OSError):
                manifest = []

        entry = dict(meta)
        entry["file"] = path.name
        manifest.append(entry)

        with open(self.manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)


# ==========================================================================
# QueryResolver
# ==========================================================================

class QueryResolver:
    """Resuelve la cadena de búsqueda para cada fuente.

    Prioridad:
      1. `search.providers.<source>.query` en config.json
      2. `queries.<source>` en conceptsQuery.json
      3. `query` genérica en conceptsQuery.json (fallback)
    """

    def __init__(self, input_dir: str | Path, search_config: Dict[str, Any]) -> None:
        self.input_dir = Path(input_dir)
        self.search_config = search_config or {}
        filename = self.search_config.get("query_source", "conceptsQuery.json")
        self.path = self.input_dir / filename
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            writeLog("warning", logger, f"[QUERY] No se encontró {self.path}")
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            writeLog("error", logger, f"[QUERY] No se pudo leer {self.path}: {exc}")
            return {}

    def for_source(self, source: str) -> tuple[str, str]:
        """Devuelve (query, origen). Origen vacío si no hay query."""
        provider_cfg = self.search_config.get("providers", {}).get(source, {})
        if provider_cfg.get("query"):
            return provider_cfg["query"], f"config.search.providers.{source}.query"

        if isinstance(self.data, dict):
            queries = self.data.get("queries")
            if isinstance(queries, dict) and queries.get(source):
                return queries[source], f"{self.path.name}:queries.{source}"
            if self.data.get("query"):
                return self.data["query"], f"{self.path.name}:query"

        return "", ""

    def per_concept_queries(self, source: str) -> list[tuple[str, str]]:
        """Queries por pregunta de investigación (decomposition).

        Devuelve ``[(etiqueta, query)]`` a partir de ``conceptsQuery.json >
        concepts[]``. Cada concepto define su propia ``query`` en lenguaje
        natural; estas queries se suman a la query genérica de la fuente para
        ampliar el recall (una consulta por RQ en lugar de una sola global).
        """
        result: list[tuple[str, str]] = []
        if not isinstance(self.data, dict):
            return result
        for concept in self.data.get("concepts", []) or []:
            if not isinstance(concept, dict):
                continue
            query = (concept.get("query") or concept.get("name") or "").strip()
            if not query:
                continue
            label = concept.get("name") or f"concept_{concept.get('id')}"
            result.append((label, query))
        return result


# ==========================================================================
# SearchProvider
# ==========================================================================

class SearchProvider(ABC):
    """Interfaz de un proveedor de búsqueda bibliográfica.

    Contrato:
      - fetch(query, max_results) -> raw serializable (se guarda en snapshot)
      - normalize(raw)            -> lista de registros canónicos
    """

    mode = "api"

    def __init__(
        self,
        source: str,
        config: Optional[Dict[str, Any]] = None,
        client: Optional[HttpClient] = None,
    ) -> None:
        self.source = source
        self.config = config or {}
        self.client = client or HttpClient()

    @abstractmethod
    def fetch(self, query: str, *, max_results: int = 500) -> Any:
        """Obtiene los datos crudos de la API."""

    @abstractmethod
    def normalize(self, raw: Any) -> List[Dict[str, Any]]:
        """Convierte los datos crudos en registros canónicos."""


# ==========================================================================
# CanonicalBuilder
# ==========================================================================

class CanonicalBuilder:
    """Construye el registro canónico común a todas las fuentes.

    Mantiene las claves que ya consumen `searchMergeEngine` y el resto del
    pipeline, y añade campos nuevos de forma aditiva.
    """

    @staticmethod
    def build(
        *,
        source: str,
        source_id: str = "",
        doi: str = "",
        title: str = "",
        abstract: str = "",
        authors: Optional[List[str]] = None,
        authors_detail: Optional[List[Dict[str, Any]]] = None,
        keywords: Optional[List[str]] = None,
        year: Optional[int] = None,
        venue: str = "",
        document_type: str = "",
        language: str = "",
        references_count: Optional[int] = None,
        citation_count: Optional[int] = None,
        pmid: str = "",
        pmcid: str = "",
        oa_url: str = "",
        references: Optional[List[str]] = None,
        pdf_url: Optional[str] = None,
        landing_page: Optional[str] = None,
        open_access: Optional[str] = None,
        source_url: Optional[str] = None,
        raw_source_file: str = "",
        snapshot_ref: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        title = normalize_text(title or "")
        doi = normalize_doi(doi or "")
        pmid = (pmid or "").strip()

        clean_authors: List[str] = []
        seen_authors = set()
        for author in authors or []:
            author = (author or "").strip()
            if author and author not in seen_authors:
                seen_authors.add(author)
                clean_authors.append(author)

        clean_keywords = sorted(
            {k.strip() for k in (keywords or []) if k and k.strip()}
        )

        if doi:
            original_id = f"doi:{doi}"
        elif pmid:
            original_id = f"pmid:{pmid}"
        else:
            original_id = f"title:{sha1(normalized_title(title))}"

        record: Dict[str, Any] = {
            "paper_id": "",
            "source": source,
            "source_index": None,
            "source_id": source_id,
            "original_id": original_id,
            "doi": doi,
            "title": title,
            "normalized_title": normalized_title(title),
            "abstract": normalize_text(abstract or ""),
            "authors": clean_authors,
            "authors_detail": authors_detail or [],
            "keywords": clean_keywords,
            "year": year,
            "venue": venue or "",
            "document_type": document_type or "",
            "language": language or "",
            "references_count": references_count,
            "citation_count": citation_count,
            "references": references or [],
            "relevance_score": None,
            "selected": False,
            "has_full_text": False,
            "raw_source_file": raw_source_file,
            "snapshot_ref": snapshot_ref,
            "pdf_url": pdf_url,
            "landing_page": landing_page,
            "pmid": pmid or None,
            "pmcid": pmcid or None,
            "oa_url": oa_url or None,
            "source_url": source_url,
            "open_access": open_access,
        }

        if extra:
            record.update(extra)

        return record
