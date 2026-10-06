"""
referenceFilter.py
==================
Utilidades compartidas para detectar y eliminar bibliografía/referencias.

Dos niveles de granularidad:
  - ``looks_like_reference(text)``: ¿este fragmento (ventana de evidencia)
    es en realidad una lista de referencias bibliográficas? Se usa para
    descartar ventanas que no son evidencia real.
  - ``strip_reference_tail(text)``: recorta la cola de referencias de un
    texto/sección completo, preservando todo el contenido previo.
"""

from __future__ import annotations

import re

# Encabezado de bibliografía. El texto puede venir con o sin saltos de línea
# (corpusCleaning colapsa el espacio en blanco), por eso se admite que el
# encabezado vaya precedido por fin de frase, salto de línea o inicio.
_REF_HEADING = re.compile(
    r"(?i)(?:[.!?]\s+|\n|^)"
    r"(references|bibliography|works cited|literature cited|references and notes)"
    r"\s*(?=[A-Z0-9\[\(]|et al)"
)

_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_CITE = re.compile(
    r"\b(doi|et al\.|vol\.|pp\.|journal|transactions|proceedings|"
    r"conference|symposium|springer|elsevier|ieee)\b|\[[0-9]{1,3}\]|https?://",
    re.IGNORECASE,
)
_SURNAME_INIT = re.compile(r"\b[A-Z][a-z]{1,},\s*[A-Z]\.")
_PAREN_YEAR = re.compile(r"\(\d{4}[a-z]?\)")

# Densidad de citas para validar que la cola es realmente bibliografía.
_MIN_TAIL_CHARS = 80
_TAIL_PROBE = 1500


def _is_citation_dense(tail: str) -> bool:
    if len(_YEAR.findall(tail)) < 3:
        return False
    return (
        len(_CITE.findall(tail)) >= 2
        or len(_SURNAME_INIT.findall(tail)) >= 3
        or len(_PAREN_YEAR.findall(tail)) >= 3
    )


def strip_reference_tail(text: str) -> str:
    """
    Devuelve ``text`` sin la cola de referencias bibliográficas.
    Recorta en el primer encabezado de bibliografía cuya cola tenga densidad
    de citas, para no eliminar contenido real (una mención suelta a
    "references" sin bibliografía detrás no se recorta).
    """
    if not text:
        return text

    best = None
    for match in _REF_HEADING.finditer(text):
        tail = text[match.end() : match.end() + _TAIL_PROBE]
        if len(tail) < _MIN_TAIL_CHARS or _is_citation_dense(tail):
            best = match
            break

    if best is None:
        return text
    return text[: best.start()].rstrip()


def looks_like_reference(text: str) -> bool:
    """
    Heurística conservadora: detecta fragmentos que son listas de referencias
    bibliográficas (autor, iniciales, año, revista, DOI...) y no evidencia real.
    Exige varias señales combinadas para evitar falsos positivos.
    """
    score = 0
    if len(re.findall(r"\b[A-Z]\.\s?(?:[A-Z]\.)?", text)) >= 4:
        score += 2
    if len(_SURNAME_INIT.findall(text)) >= 2:
        score += 2
    if len(re.findall(r"(?:^|\s)\d{1,3}\.\s+[A-Z]", text)) >= 2:
        score += 2
    if len(_YEAR.findall(text)) >= 3:
        score += 1
    if re.search(r"\bet al\.", text):
        score += 1
    if re.search(r"\bdoi|doi\.org|https?://", text, re.IGNORECASE):
        score += 1
    if re.search(r"\b(vol|no|pp)\.", text, re.IGNORECASE):
        score += 1
    if re.search(
        r"\b(proceedings|conference|symposium|journal|transactions|springer|elsevier)\b",
        text,
        re.IGNORECASE,
    ):
        score += 1
    return score >= 4
