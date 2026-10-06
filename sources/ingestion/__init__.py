"""
sources/ingestion
=================

Capa de ingesta *API-first* para SLIP.

Objetivo
--------
Sustituir (o complementar) los frágiles parsers de fichero exportado por
clientes de las APIs oficiales de las bases de datos. La estrategia es:

    fetch (red)  ->  snapshot crudo en disco  ->  normalize (offline)

El snapshot crudo permite reproducir la normalización sin volver a llamar a
la API, preservando la auditabilidad tipo PRISMA.

Fuentes soportadas en esta fase: PubMed, Scopus e IEEE Xplore. WoS queda en
modo `file` hasta disponer de API key.
"""

from sources.ingestion.base import (
    CanonicalBuilder,
    HttpClient,
    IngestionError,
    MissingCredentials,
    QueryResolver,
    SearchProvider,
    SnapshotStore,
)

__all__ = [
    "CanonicalBuilder",
    "HttpClient",
    "IngestionError",
    "MissingCredentials",
    "QueryResolver",
    "SearchProvider",
    "SnapshotStore",
]
