#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
acl_anthology_provider.py
=========================

Proveedor de **ACL Anthology** (https://aclanthology.org/): el repositorio de
referencia de publicaciones en Procesamiento del Lenguaje Natural (ACL,
EMNLP, NAACL, EACL, COLING, CONLL, etc.).

A diferencia de las otras fuentes, ACL Anthology no expone una API REST
convencional. Se accede a través de la librería Python ``acl_anthology``,
que clona (o reusa) el repositorio git del Anthology y permite iterar sobre
todos sus papers con metadatos estructurados (título, abstract, autores,
año, venue, DOI, PDF…).

Campos extraídos: ACL ID, título, abstract, autores, año, venue,
document_type ("conference"), idioma ("en"), pdf_url, landing_page, DOI.

Configuración esperada en ``config.json > defaults.search.providers.acl_anthology``:

    {
        "mode": "api",
        "repo_path": "./acl-anthology",          # opcional: ruta al repo clonado
        "min_year": 2022,                        # filtro por año mínimo
        "max_results": 500                       # tope de resultados por query
    }

Si no se indica ``repo_path``, la librería intentará clonar el repositorio
oficial en un directorio temporal bajo ``results/input/<subject>/_raw/acl_anthology``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from sources.common.common import logger, writeLog
from sources.ingestion.base import CanonicalBuilder, IngestionError, SearchProvider

ACL_ANTHOLOGY_REPO_URL = "https://github.com/acl-org/acl-anthology.git"


def _tokenize_query(query: str) -> List[str]:
    """Tokeniza una query en: paréntesis (con su contenido como bloque),
    operadores AND/OR y frases entre comillas.

    Cuando se encuentra ``(``, captura todo hasta el cierre ``)`` como un
    solo token, de modo que operadores como ``|`` dentro de paréntesis se
    conservan intactos para ser compilados como expresión regular.
    """
    tokens: List[str] = []
    i = 0
    n = len(query)
    while i < n:
        ch = query[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and query[j] != '"':
                j += 1
            tokens.append(query[i : j + 1])
            i = j + 1
        elif ch == "(":
            # Capturar todo hasta el cierre ) como un solo bloque (incluye |)
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if query[j] == "(":
                    depth += 1
                elif query[j] == ")":
                    depth -= 1
                j += 1
            tokens.append(query[i:j])
            i = j
        elif ch == ")":
            # Paréntesis de cierre suelto (no debería pasar normalmente)
            tokens.append(ch)
            i += 1
        else:
            j = i
            while j < n and not query[j].isspace() and query[j] not in '()"':
                j += 1
            tokens.append(query[i:j])
            i = j
    return tokens


def _parse_query_to_dnf(tokens: List[str]) -> List[List[str]]:
    """
    Convierte los tokens de una query en DNF (Forma Normal Disyuntiva).

    Devuelve una lista de grupos. Cada grupo es una lista de términos que
    deben coincidir TODOS (AND dentro del grupo). Los grupos se combinan con
    OR entre ellos.

    Soporta:
      - (A OR B) AND (C OR D)  →  [[A,C],[A,D],[B,C],[B,D]]
      - A AND (B OR C) AND D   →  [[A,B,D],[A,C,D]]
      - A OR B OR C            →  [[A],[B],[C]]
      - (A OR B OR C)          →  [[A],[B],[C]]
      - A B C                  →  [[A,B,C]]  (AND implícito)
    """
    if not tokens:
        return [[]]

    tree, _ = _parse_expr(tokens, 0)
    return _ast_to_dnf(tree)


def _parse_expr(tokens: List[str], pos: int) -> tuple[Any, int]:
    """Parsea tokens en un AST anidado según paréntesis.

    Devuelve (nodo, nueva_posición). Un nodo puede ser:
      - str: un término
      - list: una secuencia de ítems y operadores AND/OR
    """
    nodes: List[Any] = []
    while pos < len(tokens):
        tok = tokens[pos]
        if tok == ")":
            return nodes, pos + 1
        elif tok == "(":
            sub_nodes, pos = _parse_expr(tokens, pos + 1)
            nodes.append(sub_nodes)
        elif tok.upper() in ("AND", "OR"):
            nodes.append(tok.upper())
            pos += 1
        else:
            nodes.append(tok)
            pos += 1
    return nodes, pos


def _ast_to_dnf(node: Any) -> List[List[str]]:
    """Convierte un nodo AST a DNF: lista de grupos (AND dentro, OR entre)."""
    if isinstance(node, str):
        return [[node]]

    if isinstance(node, list):
        # Dividir por OR (operador de menor precedencia) para obtener
        # alternativas de nivel superior.
        or_parts: List[List[Any]] = []
        current: List[Any] = []
        for item in node:
            if item == "OR":
                or_parts.append(current)
                current = []
            else:
                current.append(item)
        or_parts.append(current)

        dnf: List[List[str]] = []
        for part in or_parts:
            dnf.extend(_expand_and_part(part))
        return dnf if dnf else [[]]

    return [[]]


def _expand_and_part(items: List[Any]) -> List[List[str]]:
    """Expande una parte conectada por AND (implícito o explícito) a DNF.

    items es una lista de operandos y opcionalmente strings "AND".
    """
    operands = [it for it in items if it != "AND"]
    if not operands:
        return [[]]

    operand_dnfs = [_ast_to_dnf(op) for op in operands]

    import itertools

    result: List[List[str]] = []
    for combo in itertools.product(*operand_dnfs):
        merged: List[str] = []
        for c in combo:
            merged.extend(c)
        result.append(merged)
    return result


def _compile_group_terms(group_terms: List[str]) -> List[re.Pattern[str]]:
    """Compila los términos de un grupo (AND implícito) en una lista de regex.

    Un término que empieza y termina con paréntesis (ej.
    ``(A|B|C)``) se compila como una expresión regular completa, conservando
    el operador ``|`` de alternation. Los demás términos se compilan como
    coincidencia de palabra entera (con soporte para comodines ``*``).

    Nota: ``json.load`` interpreta ``\\b`` como carácter de retroceso
    (backspace, \\x08). Aquí se convierte de vuelta al metacarácter de regex
    ``\\b`` (word boundary) para que funcione correctamente.
    """
    patterns: List[re.Pattern[str]] = []
    for term in group_terms:
        term = term.strip()
        if not term:
            continue
        # json.load convierte "\\b" en backspace (\x08); restaurar a \b de regex
        term = term.replace("\x08", r"\b")
        # Bloque entre paréntesis: expresión regular completa (incluye |)
        if term.startswith("(") and term.endswith(")"):
            regex_content = term[1:-1]
            patterns.append(re.compile(regex_content, re.IGNORECASE))
        elif term.startswith('"') and term.endswith('"'):
            # Frase exacta entre comillas
            phrase = term[1:-1]
            patterns.append(re.compile(re.escape(phrase), re.IGNORECASE))
        elif "*" in term:
            # Comodín simple
            term_re = term.replace("*", ".*")
            patterns.append(re.compile(term_re, re.IGNORECASE))
        else:
            # Término suelto: coincidencia de palabra entera
            patterns.append(re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    return patterns


def _matches_any_group(text: str, groups: List[List[re.Pattern[str]]]) -> bool:
    """Devuelve True si el texto satisface al menos un grupo completo (AND dentro del grupo, OR entre grupos)."""
    if not groups:
        return True
    return any(
        all(p.search(text) is not None for p in group)
        for group in groups
    )


class AclAnthologyProvider(SearchProvider):
    """Proveedor de ACL Anthology mediante la librería ``acl_anthology``."""

    mode = "api"

    def __init__(
        self,
        source: str,
        config: Optional[Dict[str, Any]] = None,
        client=None,  # noqa: ARG002  (no usa HttpClient)
    ) -> None:
        super().__init__(source, config, client=None)
        self.repo_path: Optional[str] = self.config.get("repo_path")
        self.min_year: int = int(self.config.get("min_year", 2022))
        self.max_results: int = int(self.config.get("max_results", 500))

        # Cargar mapeo de patrones de DOI → venue desde conceptsQuery.json
        self.doi_venue_patterns: list[dict[str, str]] = []
        self._load_doi_venue_patterns()

    def _load_doi_venue_patterns(self) -> None:
        """Carga el mapeo de patrones de DOI → venue desde conceptsQuery.json."""
        from pathlib import Path

        import sources.common.paramsManager as pm

        base_input = Path(pm.processControl.env.get("input", "results/input"))
        subject = getattr(pm.processControl.args, "subject", "default")
        concepts_file = base_input / subject / "conceptsQuery.json"

        if not concepts_file.exists():
            writeLog("debug", logger,
                     f"[ACL Anthology] No se encontró {concepts_file}; sin mapeo DOI→venue")
            return

        try:
            with open(concepts_file, encoding="utf-8") as f:
                data = json.load(f)
            patterns = data.get("doi_venue_patterns", [])
            if isinstance(patterns, list):
                self.doi_venue_patterns = [
                    {"pattern": str(p.get("pattern", "")),
                     "venue": str(p.get("venue", "")),
                     "category": str(p.get("category", ""))}
                    for p in patterns if isinstance(p, dict)
                ]
            writeLog("info", logger,
                     f"[ACL Anthology] {len(self.doi_venue_patterns)} patrones DOI→venue cargados")
        except (json.JSONDecodeError, OSError) as exc:
            writeLog("warning", logger,
                     f"[ACL Anthology] No se pudo leer doi_venue_patterns: {exc}")

    def _venue_from_doi(self, doi: str) -> tuple[str, str]:
        """Extrae venue y categoría a partir del patrón del DOI.

        Los DOIs de ACL Anthology tienen el formato:
        10.18653/v1/{year}.{venue_pattern}.{number}
        """
        if not doi or not self.doi_venue_patterns:
            return "", ""

        # Extraer la parte después de v1/
        parts = doi.split("/")
        if len(parts) < 3:
            return "", ""
        last_part = parts[-1]  # ej: 2023.acl-long.741

        # Dividir por punto para obtener componentes
        components = last_part.split(".")
        # El venue pattern suele estar en components[1] (después del año)
        venue_component = components[1] if len(components) > 1 else ""

        for entry in self.doi_venue_patterns:
            pattern = entry["pattern"]
            # Patrón con guion al final (codi-, law-, etc.) = prefijo del venue component
            if pattern.endswith("-"):
                if venue_component.startswith(pattern):
                    return entry["venue"], entry["category"]
            else:
                # Patrón exacto: verificar si está contenido en el venue component
                if pattern in venue_component:
                    return entry["venue"], entry["category"]

        return "", ""

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------
    def fetch(self, query: str, *, max_results: int = 500) -> Dict[str, Any]:
        """Busca papers en ACL Anthology que coincidan con la query.

        Dado que ``acl_anthology`` no tiene endpoint de búsqueda, se itera
        sobre todos los papers del repositorio y se filtran los que coincidan
        con todos los términos de la query y con el año mínimo.
        """
        try:
            from acl_anthology import Anthology  # type: ignore
        except ImportError as exc:
            raise IngestionError(
                "La librería 'acl_anthology' no está instalada. "
                "Añádela a requirements.txt e instálala con pip."
            ) from exc

        # Resolver ruta del repositorio. Por defecto se usa
        # sources/third_party/acl-anthology (compartido por todas las investigaciones).
        repo_path = self.repo_path
        if not repo_path:
            from pathlib import Path

            # Resolver la raíz del proyecto:
            # este archivo está en sources/ingestion/, así que parents[2] es la raíz.
            project_root = Path(__file__).resolve().parents[2]
            repo_path = str(project_root / "sources" / "third_party" / "acl-anthology")

        # Si el repositorio no existe, intentar clonarlo automáticamente.
        if not Path(repo_path).exists():
            writeLog("info", logger,
                     f"[ACL Anthology] Repositorio no encontrado en {repo_path}; clonando...")
            try:
                import git  # type: ignore
                git.Repo.clone_from(ACL_ANTHOLOGY_REPO_URL, repo_path)
                writeLog("info", logger,
                         f"[ACL Anthology] Repositorio clonado en {repo_path}")
            except ImportError:
                raise IngestionError(
                    f"El repositorio ACL Anthology no existe en {repo_path} y no se puede clonar: "
                    f"falta la librería 'gitpython'. Instálela con: pip install gitpython"
                )
            except Exception as exc:
                raise IngestionError(
                    f"No se pudo clonar el repositorio ACL Anthology en {repo_path}: {exc}"
                ) from exc

        writeLog("info", logger,
                 f"[ACL Anthology] Abriendo repositorio en {repo_path} ...")

        try:
            anthology = Anthology.from_repo(repo_path)
        except Exception as exc:
            raise IngestionError(
                f"No se pudo abrir el repositorio ACL Anthology en {repo_path}: {exc}"
            ) from exc

        groups_terms = _parse_query_to_dnf(_tokenize_query(query))
        groups = [_compile_group_terms(g) for g in groups_terms]
        max_results = max_results or self.max_results

        hits: List[Dict[str, Any]] = []
        count = 0
        for paper in anthology.papers():
            count += 1
            try:
                year = int(paper.year) if paper.year else 0
            except (ValueError, TypeError):
                year = 0

            if year < self.min_year:
                continue

            text = f"{paper.title or ''} {paper.abstract or ''}"
            if not _matches_any_group(text, groups):
                continue

            doi = str(getattr(paper, "doi", "") or "")
            venue_from_doi, venue_category = self._venue_from_doi(doi)

            hits.append({
                "full_id": str(paper.full_id or ""),
                "title": str(paper.title or ""),
                "abstract": str(paper.abstract or ""),
                "year": year,
                "venue": str(getattr(paper, "venue", "") or ""),
                "venue_from_doi": venue_from_doi,
                "venue_category": venue_category,
                "authors": [str(a.name or "") for a in (paper.authors or [])],
                "doi": doi,
                "pdf_url": str(getattr(paper, "pdf_url", "") or ""),
                "landing_page": str(getattr(paper, "url", "") or ""),
                "anthology_id": str(paper.full_id or ""),
            })

            if len(hits) >= max_results:
                break

        writeLog("info", logger,
                 f"[ACL Anthology] {count} papers revisados, {len(hits)} coinciden con la query "
                 f"(año >= {self.min_year})")

        return {
            "query": query,
            "repo_path": repo_path,
            "min_year": self.min_year,
            "total_reviewed": count,
            "hits": hits,
        }

    # ------------------------------------------------------------------
    # normalize
    # ------------------------------------------------------------------
    def normalize(self, raw: Any) -> List[Dict[str, Any]]:
        """Convierte los hits crudos de ACL Anthology en registros canónicos."""
        if not isinstance(raw, dict):
            writeLog("warning", logger, "[ACL Anthology] Raw no es un dict; se ignora")
            return []

        hits = raw.get("hits", [])
        if not hits:
            return []

        records: List[Dict[str, Any]] = []
        for hit in hits:
            record = CanonicalBuilder.build(
                source=self.source,
                source_id=hit.get("anthology_id", ""),
                doi=hit.get("doi", ""),
                title=hit.get("title", ""),
                abstract=hit.get("abstract", ""),
                authors=hit.get("authors", []),
                year=hit.get("year"),
                venue=hit.get("venue", ""),
                document_type="conference",
                language="en",
                references_count=None,
                citation_count=None,
                pmid="",
                pmcid="",
                oa_url="",
                pdf_url=hit.get("pdf_url") or None,
                landing_page=hit.get("landing_page") or None,
                open_access="open",
                source_url=hit.get("landing_page") or "",
                extra={
                    "acl_id": hit.get("full_id", ""),
                    "anthology_id": hit.get("anthology_id", ""),
                    "venue_from_doi": hit.get("venue_from_doi", ""),
                    "venue_category": hit.get("venue_category", ""),
                },
            )
            records.append(record)

        writeLog("info", logger,
                 f"[ACL Anthology] normalizados {len(records)} registros")
        return records
