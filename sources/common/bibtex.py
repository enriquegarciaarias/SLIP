"""
bibtex.py
=========
Extracción de metadatos bibliográficos y generación de BibTeX a partir de PDFs.

Arquitectura en capas (SOA):
  ┌──────────────────────────────────────────────┐
  │         BibTeXService  (fachada pública)      │
  ├──────────────┬───────────────────────────────┤
  │  PdfReader   │  MetadataExtractor            │
  │  (I/O PDF)   │  (heurísticas + LLM fallback) │
  ├──────────────┴───────────────────────────────┤
  │           BibTeXFormatter                    │
  │     (clave BibTeX + entrada formateada)      │
  └──────────────────────────────────────────────┘

Principios aplicados:
  - SRP  : cada clase tiene una sola razón para cambiar
  - OCP  : MetadataExtractor es extensible sin modificar el servicio
  - DIP  : BibTeXService recibe LLMClient por inyección
  - Tipos estrictos: Path en lugar de str|Path mixto
  - Sin estado global mutable (_llm_client eliminado)
  - Pydantic v2 field_validator en lugar del deprecado @validator
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import fitz  # PyMuPDF
from pydantic import BaseModel, Field, field_validator

from sources.common.common import logger, writeLog
from sources.common.llm_client import LLMClient, create_ollama_client


# ---------------------------------------------------------------------------
# Modelo de dominio
# ---------------------------------------------------------------------------

class BibMetadata(BaseModel):
    title: str = Field(..., description="Título del artículo")
    authors: List[str] = Field(default_factory=list, description="Lista de autores")
    year: int = Field(2026, description="Año de publicación")
    doi: Optional[str] = Field(None, description="DOI si existe")
    conference: str = Field(
        "Working Notes of CLEF 2026", description="Nombre de la conferencia"
    )

    @field_validator("year")
    @classmethod
    def check_year(cls, v: int) -> int:
        if not (2000 <= v <= 2030):
            raise ValueError(f"Year {v} fuera de rango esperado (2000-2030)")
        return v


# Resultado final que devuelve el servicio
@dataclass(frozen=True)
class BibTeXResult:
    metadata: BibMetadata
    bibtex: str
    bib_key: str


# ---------------------------------------------------------------------------
# Capa 1 – Lectura de PDF (I/O aislado)
# ---------------------------------------------------------------------------

class PdfReader:
    """
    Responsabilidad única: abrir un PDF y devolver texto y metadatos crudos.
    Aísla el I/O de fitz para que el resto del módulo sea testeable sin disco.
    """

    def read(self, pdf_path: Path) -> tuple[str, Dict[str, str]]:
        """
        Devuelve (texto_completo, metadatos_crudos).
        Abre el fichero una sola vez.
        """
        with fitz.open(pdf_path) as doc:
            raw_meta: Dict[str, str] = doc.metadata or {}
            text = "".join(page.get_text() for page in doc)
        return text, raw_meta


# ---------------------------------------------------------------------------
# Capa 2 – Extracción de metadatos (heurísticas + LLM)
# ---------------------------------------------------------------------------

class MetadataExtractor:
    """
    Extrae BibMetadata desde texto y metadatos PDF crudos.
    Estrategia: heurísticas rápidas primero; LLM sólo como fallback.
    """

    _DOI_PATTERN = re.compile(r"10\.\d{4,}/\S+")
    _YEAR_PATTERN = re.compile(r"(20\d{2})")
    _PDF_DATE_PATTERN = re.compile(r"D:(\d{4})")
    _FULLNAME_PATTERN = re.compile(
        r"^[A-Z][a-z]+(?: [A-Z][a-z]+)+$"
    )
    _INITIALS_PATTERN = re.compile(r"^[A-Z]\. [A-Z][a-z]+")

    _DEFAULT_CONFERENCE = "Working Notes of CLEF 2026"
    _LLM_SYSTEM_PROMPT = "You are a JSON generator that returns a JSON object."
    _LLM_PROMPT_TEMPLATE = (
        "Extract the following bibliographic metadata from this academic paper text.\n"
        "Return a JSON object with these exact keys:\n"
        '  "title"      : full title (string)\n'
        '  "authors"    : list of author full names (list of strings)\n'
        '  "year"       : publication year (integer)\n'
        '  "doi"        : DOI if present, else null\n'
        '  "conference" : conference name\n\n'
        "IMPORTANT: Respond ONLY with the JSON object. No other text.\n\n"
        "Text:\n{text}"
    )

    def __init__(self, llm_client: LLMClient, default_conference: str = _DEFAULT_CONFERENCE) -> None:
        self._llm = llm_client
        self._default_conference = default_conference

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def extract(self, text: str, raw_meta: Dict[str, str], context: str = "") -> BibMetadata:
        """
        Intenta extraer metadatos con heurísticas.
        Si el resultado es incompleto, cae en el LLM.
        Nunca devuelve None: en el peor caso retorna defaults razonables.
        """
        heuristic = self._extract_heuristic(text, raw_meta)

        # Si el título sigue siendo placeholder o no hay autores, usar LLM
        if heuristic.title == "Unknown Title" or not heuristic.authors:
            writeLog("info", logger,
                     f"[MetadataExtractor] Heuristics incomplete for {context}, calling LLM")
            llm_result = self._extract_with_llm(text, context)
            if llm_result:
                return llm_result

        return heuristic

    # ------------------------------------------------------------------
    # Estrategia 1: heurísticas sobre PDF metadata + texto
    # ------------------------------------------------------------------

    def _extract_heuristic(self, text: str, raw_meta: Dict[str, str]) -> BibMetadata:
        title = self._extract_title(text, raw_meta)
        authors = self._extract_authors(text, raw_meta)
        year = self._extract_year(text, raw_meta)
        doi = self._extract_doi(text, raw_meta)

        return BibMetadata(
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            conference=self._default_conference,
        )

    def _extract_title(self, text: str, raw_meta: Dict[str, str]) -> str:
        title = raw_meta.get("title", "").strip()
        if title:
            return title
        return self._title_from_text(text) or "Unknown Title"

    def _extract_authors(self, text: str, raw_meta: Dict[str, str]) -> List[str]:
        raw = raw_meta.get("author", "").strip()
        if raw:
            return self._split_author_string(raw)
        return self._authors_from_text(text)

    def _extract_year(self, text: str, raw_meta: Dict[str, str]) -> int:
        # 1. Fecha de creación del PDF
        date_str = raw_meta.get("creationDate", "")
        match = self._PDF_DATE_PATTERN.search(date_str)
        if match:
            return int(match.group(1))
        # 2. Primera mención de año en el texto
        match = self._YEAR_PATTERN.search(text)
        if match:
            return int(match.group(1))
        return 2026

    def _extract_doi(self, text: str, raw_meta: Dict[str, str]) -> Optional[str]:
        match = self._DOI_PATTERN.search(text)
        if match:
            return match.group(0)
        # Algunos PDFs guardan el DOI en el campo keywords
        match = self._DOI_PATTERN.search(raw_meta.get("keywords", ""))
        if match:
            return match.group(0)
        return None

    # ------------------------------------------------------------------
    # Estrategia 2: LLM como fallback
    # ------------------------------------------------------------------

    def _extract_with_llm(self, text: str, context: str) -> Optional[BibMetadata]:
        prompt = self._LLM_PROMPT_TEMPLATE.format(text=text[:4000])
        return self._llm.generate_structured(
            prompt=prompt,
            model_class=BibMetadata,
            system_prompt=self._LLM_SYSTEM_PROMPT,
            context=f"bibtex-metadata:{context}",
        )

    # ------------------------------------------------------------------
    # Helpers de parsing de texto
    # ------------------------------------------------------------------

    @staticmethod
    def _title_from_text(text: str) -> Optional[str]:
        """Heurística simple: primera línea con más de 20 chars que no sea sólo dígitos."""
        for line in text.split("\n")[:20]:
            line = line.strip()
            if (
                len(line) > 20
                and not re.match(r"^\d+$", line)
                and not re.match(r"^[A-Z\s]+$", line)
            ):
                return line
        return None

    def _authors_from_text(self, text: str) -> List[str]:
        """Extrae líneas que parecen nombres de autores en las primeras 30 líneas."""
        authors: List[str] = []
        for line in text.split("\n")[:30]:
            line = line.strip()
            if self._FULLNAME_PATTERN.match(line) or self._INITIALS_PATTERN.match(line):
                authors.append(line)
        return authors

    @staticmethod
    def _split_author_string(raw: str) -> List[str]:
        """Divide una cadena de autores por ';' o ' and '."""
        if ";" in raw:
            return [a.strip() for a in raw.split(";") if a.strip()]
        if " and " in raw:
            return [a.strip() for a in raw.split(" and ") if a.strip()]
        return [raw]


# ---------------------------------------------------------------------------
# Capa 3 – Formateo BibTeX (sin I/O, sin LLM, pura transformación)
# ---------------------------------------------------------------------------

class BibTeXFormatter:
    """
    Responsabilidad única: transformar BibMetadata en una entrada BibTeX válida.
    Sin dependencias externas; fácilmente testeable de forma aislada.
    """

    _NON_ALNUM = re.compile(r"[^a-zA-Z0-9\s]")

    def format(self, metadata: BibMetadata) -> BibTeXResult:
        """Genera la clave y la entrada BibTeX completa."""
        key = self._build_key(metadata.authors, metadata.year, metadata.title)
        entry = self._build_entry(key, metadata)
        return BibTeXResult(metadata=metadata, bibtex=entry, bib_key=key)

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _build_key(self, authors: List[str], year: int, title: str) -> str:
        """apellido1_año_3palabras  (e.g. smith_2024_neuralInfo)"""
        if not authors:
            return f"unknown_{year}"
        last_name = authors[0].strip().split()[-1].lower()
        words = self._NON_ALNUM.sub("", title).split()[:3]
        key_suffix = "".join(w[:3] for w in words) if words else "paper"
        return f"{last_name}_{year}_{key_suffix}"

    @staticmethod
    def _format_authors(authors: List[str]) -> str:
        """Convierte ["First Last"] a "Last, First" en formato BibTeX."""
        formatted: List[str] = []
        for author in authors:
            parts = author.strip().split()
            if len(parts) >= 2:
                formatted.append(f"{' '.join(parts[1:])}, {parts[0]}")
            else:
                formatted.append(author)
        return " and ".join(formatted)

    def _build_entry(self, key: str, m: BibMetadata) -> str:
        author_str = self._format_authors(m.authors)
        lines = [
            f"@inproceedings{{{key},",
            f"  author    = {{{author_str}}},",
            f"  title     = {{{m.title}}},",
            f"  booktitle = {{{m.conference}}},",
            f"  year      = {{{m.year}}},",
        ]
        if m.doi:
            lines.append(f"  doi       = {{{m.doi}}},")
        lines.append("}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Capa 4 – Fachada del servicio (punto de entrada único)
# ---------------------------------------------------------------------------

class BibTeXService:
    """
    Fachada SOA: orquesta PdfReader → MetadataExtractor → BibTeXFormatter.
    Recibe sus dependencias por inyección; no instancia nada internamente.
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        default_conference: str = "Working Notes of CLEF 2026",
    ) -> None:
        client = llm_client or create_ollama_client(model="qwen3:8b")
        self._reader = PdfReader()
        self._extractor = MetadataExtractor(
            llm_client=client,
            default_conference=default_conference,
        )
        self._formatter = BibTeXFormatter()

    def extract_from_pdf(
        self,
        pdf_path: Path,
        pdf_text: Optional[str] = None,
    ) -> Optional[BibTeXResult]:
        """
        Extrae metadatos de un PDF y genera la entrada BibTeX.

        Args:
            pdf_path:  Ruta al fichero PDF (siempre Path, nunca str).
            pdf_text:  Texto ya extraído (evita releer el disco si disponible).

        Returns:
            BibTeXResult con metadata, bibtex y bib_key, o None si falla.
        """
        context = pdf_path.name
        try:
            raw_meta: Dict[str, str] = {}

            if pdf_text is None:
                pdf_text, raw_meta = self._reader.read(pdf_path)
            else:
                # Sólo necesitamos los metadatos del PDF, no re-extraer texto
                _, raw_meta = self._reader.read(pdf_path)

            metadata = self._extractor.extract(pdf_text, raw_meta, context=context)
            return self._formatter.format(metadata)

        except Exception as exc:
            writeLog("error", logger,
                     f"[BibTeXService] Failed to process {context}: {exc}")
            return None


# ---------------------------------------------------------------------------
# Helpers de conveniencia (compatibilidad con código existente)
# ---------------------------------------------------------------------------

def extract_bibtex_from_pdf(
    pdf_path: Path | str,
    pdf_text: Optional[str] = None,
    llm_client: Optional[LLMClient] = None,
) -> Optional[Dict]:
    """
    Función de conveniencia que mantiene la firma original.
    Acepta str o Path; internamente siempre usa Path.

    Returns:
        {"metadata": dict, "bibtex": str, "bib_key": str} o None.
    """
    service = BibTeXService(llm_client=llm_client)
    result = service.extract_from_pdf(Path(pdf_path), pdf_text=pdf_text)
    if result is None:
        return None
    return {
        "metadata": result.metadata.model_dump(),
        "bibtex": result.bibtex,
        "bib_key": result.bib_key,
    }


def extract_bib_key(bibtex_entry: str) -> Optional[str]:
    """Extrae la clave BibTeX de una entrada @inproceedings{key, ...}."""
    if not bibtex_entry:
        return None
    match = re.search(r"@\w+\{([^,]+),", bibtex_entry)
    return match.group(1).strip() if match else None