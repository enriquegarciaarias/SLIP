r"""
manuscriptEvidence.py
=====================
Soporte para el manuscrito del investigador dentro del pipeline SLIP.

Responsabilidades (SOA / SRP):
  1. Parsear la bibliografía LaTeX (.bib) del manuscrito.
  2. Extraer los contextos de cita del manuscrito (.tex), es decir, el texto
     que menciona cada referencia (\cite{key} -> frase que la contiene).
  3. Asociar cada PDF depositado en manual_papers/manuscript/ con su clave
     bibliográfica (bibkey), de forma determinista cuando el fichero se nombra
     con la bibkey y heurística (DOI/título, y bóveda _pdf_vault) en su defecto.
  4. Construir `manuscript_refs.json`: el contrato que consume
     trainingMaterials para emitir la sección "Evidencias en el manuscrito".
  5. Generar, con grounding en el propio paper y caché en disco, la pequeña
     "extensión de la cita" que ayuda al investigador a recordar qué aportaba
     el paper originalmente (con fallback extractivo sin LLM).

No genera Markdown: eso es responsabilidad de trainingMaterials.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz

from sources.common.common import logger, processControl, writeLog
from sources.common.utils import sha1

# ---------------------------------------------------------------------------
# Nombres esperados dentro de manual_papers/manuscript/
# ---------------------------------------------------------------------------

MANUSCRIPT_DIRNAME = "manuscript"
MANUSCRIPT_SUBDIR = Path("manual_papers") / MANUSCRIPT_DIRNAME
MAIN_PDF_NAME = "manuscript.pdf"
REFS_FILE_NAME = "manuscript_refs.json"
CACHE_FILE_NAME = "manuscript_context_cache.json"

_TEX_CANDIDATES = ("manuscript.tex", "main.tex")
_BIB_CANDIDATES = ("manuscript.bib", "biblio.bib")

_CITE_COMMAND = re.compile(
    r"\\(?:cite|citep|citet|citealp|citeauthor|citeyear|Cite|parencite|textcite|autocite)"
    r"\*?(?:\[[^\]]*\])*\{([^}]*)\}"
)
_DOI_PATTERN = re.compile(r"(?:https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/[^\s\"<>{}]+)")


# ---------------------------------------------------------------------------
# Utilidades de parámetros de ejecución
# ---------------------------------------------------------------------------

def _flag_value(name: str, default: int) -> int:
    """
    Resuelve un flag de ejecución (--manuscript / --emergente).

    Precedencia: argumento de línea de comandos -> config.json
    (defaults.training_materials) -> valor por defecto.
    """
    args = getattr(processControl, "args", None)
    value = getattr(args, name, None) if args is not None else None
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass

    defaults = getattr(processControl, "defaults", None) or {}
    if isinstance(defaults, dict):
        section = defaults.get("training_materials", {})
        if isinstance(section, dict) and name in section:
            try:
                return int(section[name])
            except (TypeError, ValueError):
                pass
    return default


def manuscript_enabled() -> bool:
    return _flag_value("manuscript", 0) == 1


def emerging_enabled() -> bool:
    return _flag_value("emergente", 1) != 0


# ---------------------------------------------------------------------------
# Parser mínimo de BibTeX
# ---------------------------------------------------------------------------

def _strip_outer_braces(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == "{" and value[-1] == "}":
        return value[1:-1]
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _bib_clean(value: str) -> str:
    value = _strip_outer_braces(value)
    value = value.replace("\\&", "&")
    value = re.sub(r"\\[a-zA-Z]+\s*", "", value)  # \'{e} -> {e} -> e (suficiente)
    value = value.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", value).strip()


def _split_top_level(text: str, sep: str = ",") -> List[str]:
    """Divide por `sep` ignorando los que están dentro de llaves o comillas."""
    parts, depth, current, in_quotes, escaped = [], 0, [], False, False
    for ch in text:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if ch == "\\":
            current.append(ch)
            escaped = True
            continue
        if ch == '"' and depth == 0:
            in_quotes = not in_quotes
        elif ch == "{" and not in_quotes:
            depth += 1
        elif ch == "}" and not in_quotes:
            depth = max(0, depth - 1)
        if ch == sep and depth == 0 and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def parse_bib(bib_path: Path) -> Dict[str, Dict]:
    """Devuelve {bibkey: {title, doi, booktitle, author, year}}."""
    entries: Dict[str, Dict] = {}
    if not bib_path or not Path(bib_path).exists():
        return entries

    text = Path(bib_path).read_text(encoding="utf-8", errors="ignore")
    idx = 0
    while idx < len(text):
        at = text.find("@", idx)
        if at < 0:
            break
        brace = text.find("{", at)
        if brace < 0:
            break
        depth, end = 0, brace
        while end < len(text):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        body = text[brace + 1:end]
        idx = end + 1

        fields = _split_top_level(body)
        if not fields:
            continue
        key = fields[0].strip()
        if not key or "=" in key:
            continue
        record = {"key": key}
        for raw_field in fields[1:]:
            if "=" not in raw_field:
                continue
            name, _, val = raw_field.partition("=")
            name = name.strip().lower()
            if name in ("title", "doi", "booktitle", "author", "year", "crossref"):
                record[name] = _bib_clean(val)
        entries[key] = record

    # Resolver títulos vacíos usando el crossref (booktitle del contenedor)
    for record in entries.values():
        if not record.get("title"):
            cross = record.get("crossref")
            if cross and cross in entries:
                record["title"] = entries[cross].get("title", "")
    return entries


# ---------------------------------------------------------------------------
# Extracción de contextos de cita desde el .tex
# ---------------------------------------------------------------------------

def _clean_latex(context: str) -> str:
    """Suaviza el LaTeX del contexto para que sea legible en Markdown."""
    context = _CITE_COMMAND.sub(
        lambda m: "[" + m.group(1).replace(",", ", ") + "]", context
    )
    context = re.sub(r"\\(?:emph|textit|textbf|texttt|textrm)\{([^}]*)\}", r"\1", context)
    context = re.sub(r"\\[,;:!]", "", context)  # espaciados finos (\ , \; \: \!)
    context = context.replace("~", " ").replace("\\%", "%").replace("\\&", "&")
    context = context.replace("\\", " ")
    context = re.sub(r"\s+", " ", context).strip()
    return context.lstrip(" ,;:.)]—-")


_ABBREV_TAIL = re.compile(
    r"(?:\b(?:al|eq|ref|fig|tab|no|vol|pp|vs|etc|ca|approx|cf)\.|"
    r"\b[A-Z]\.|(?:i\.e|e\.g)\.)$",
    re.IGNORECASE,
)


def _is_sentence_boundary(text: str, idx: int) -> bool:
    """True si `text[idx]` cierra una frase (evita decimales y abreviaturas)."""
    char = text[idx]
    if char == "\n":
        return True
    if char not in ".!?":
        return False
    nxt = text[idx + 1] if idx + 1 < len(text) else ""
    if nxt and not (nxt.isspace() or nxt in "\"'”’)]}"):
        return False  # decimal (0.98) o palabra pegada
    if _ABBREV_TAIL.search(text[max(0, idx - 6): idx + 1]):
        return False
    return True


def _extract_context(text: str, start: int, end: int, max_len: int = 700) -> str:
    left = start
    while left > 0 and not _is_sentence_boundary(text, left - 1):
        left -= 1
    right = end
    while right < len(text) and not _is_sentence_boundary(text, right):
        right += 1
    if right < len(text):
        right += 1
    return text[left:right][:max_len]


def extract_citation_contexts(tex_path: Path) -> Dict[str, List[str]]:
    """Devuelve {bibkey: [contextos de cita ordenados por aparición]}."""
    contexts: Dict[str, List[str]] = {}
    if not tex_path or not Path(tex_path).exists():
        return contexts

    text = Path(tex_path).read_text(encoding="utf-8", errors="ignore")
    seen: Dict[str, set] = {}
    for match in _CITE_COMMAND.finditer(text):
        keys = [k.strip() for k in match.group(1).split(",") if k.strip()]
        if not keys:
            continue
        context = _clean_latex(_extract_context(text, match.start(), match.end()))
        if not context:
            continue
        for key in keys:
            bucket = contexts.setdefault(key, [])
            fingerprints = seen.setdefault(key, set())
            fingerprint = sha1(context[:200])
            if fingerprint not in fingerprints:
                fingerprints.add(fingerprint)
                bucket.append(context)
    return contexts


# ---------------------------------------------------------------------------
# Asociación PDF <-> bibkey
# ---------------------------------------------------------------------------

def _normalized_title(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _pdf_probe(pdf_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Extrae (doi, texto de cabecera) de un PDF para el matching heurístico."""
    try:
        with fitz.open(pdf_path) as doc:
            head = "".join(page.get_text() for page in doc[:3])
    except Exception as exc:  # pragma: no cover - dependiente del PDF
        writeLog("debug", logger, f"[Manuscript] No se pudo leer {pdf_path.name}: {exc}")
        return None, None
    doi_match = _DOI_PATTERN.search(head)
    doi = doi_match.group(1).rstrip(".,;:()'\">") if doi_match else None
    return doi, head[:4000]


def _vault_hash_index(output_dir: Optional[Path]) -> Dict[str, str]:
    """Invierte pdf_inventory.json -> {hash.pdf: global_id}."""
    index: Dict[str, str] = {}
    if not output_dir:
        return index
    inventory_file = Path(output_dir) / "pdf_inventory.json"
    if not inventory_file.exists():
        return index
    try:
        inventory = json.loads(inventory_file.read_text(encoding="utf-8"))
    except Exception:
        return index
    if isinstance(inventory, dict):
        for global_id, filename in inventory.items():
            if filename:
                index[filename] = global_id
    return index


def match_pdf_to_bibkey(
    pdf_path: Path,
    bib: Dict[str, Dict],
    vault_index: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], str]:
    """
    Devuelve (bibkey, método). Métodos: filename | vault | doi | title | none.
    """
    stem = pdf_path.stem
    if stem in bib:
        return stem, "filename"

    # Bóveda: paper_<hash>.pdf -> global_id -> doi/título
    if vault_index:
        global_id = vault_index.get(pdf_path.name) or vault_index.get(stem + ".pdf")
        if global_id:
            gid = global_id.lower()
            for key, record in bib.items():
                doi = (record.get("doi") or "").lower()
                if doi and gid == f"doi:{doi}":
                    return key, "vault"
            title = _normalized_title(global_id.split("title:", 1)[-1])
            if title:
                for key, record in bib.items():
                    if _normalized_title(record.get("title", "")) == title:
                        return key, "vault"

    doi, head = _pdf_probe(pdf_path)
    if doi:
        doi_low = doi.lower()
        for key, record in bib.items():
            if (record.get("doi") or "").lower() == doi_low:
                return key, "doi"

    if head:
        head_norm = _normalized_title(head)
        for key, record in sorted(bib.items(), key=lambda kv: -len(kv[1].get("title", ""))):
            title_norm = _normalized_title(record.get("title", ""))
            if len(title_norm) >= 25 and title_norm in head_norm:
                return key, "title"

    return None, "none"


# ---------------------------------------------------------------------------
# Construcción del contrato manuscript_refs.json
# ---------------------------------------------------------------------------

def find_manuscript_dir(input_dir: Path) -> Path:
    return Path(input_dir) / MANUSCRIPT_SUBDIR


def _pick_file(manuscript_dir: Path, preferred: Tuple[str, ...], glob: str) -> Optional[Path]:
    for name in preferred:
        candidate = manuscript_dir / name
        if candidate.exists():
            return candidate
    matches = sorted(manuscript_dir.glob(glob))
    return matches[0] if matches else None


def collect_manuscript_pdfs(manuscript_dir: Path) -> List[Path]:
    """PDFs asociados al manuscrito, excluyendo el propio manuscript.pdf."""
    if not manuscript_dir.is_dir():
        return []
    return sorted(
        pdf for pdf in manuscript_dir.glob("*.pdf")
        if pdf.name.lower() != MAIN_PDF_NAME.lower()
    )


def build_manuscript_refs(
    input_dir: Path,
    output_dir: Optional[Path] = None,
    persist: bool = True,
) -> Optional[Dict]:
    """
    Genera el contrato de referencias del manuscrito. Devuelve None si no hay
    manuscrito (o no está activado). Escribe `manuscript_refs.json` en output.
    """
    if not manuscript_enabled():
        return None

    manuscript_dir = find_manuscript_dir(input_dir)
    if not manuscript_dir.is_dir():
        writeLog("info", logger, f"[Manuscript] No existe {manuscript_dir}. Skipping.")
        return None

    if not (manuscript_dir / MAIN_PDF_NAME).exists():
        writeLog("warning", logger,
                 f"[Manuscript] Falta {MAIN_PDF_NAME} en {manuscript_dir}. Skipping.")
        return None

    pdfs = collect_manuscript_pdfs(manuscript_dir)
    if not pdfs:
        writeLog("warning", logger,
                 f"[Manuscript] No hay PDFs asociados en {manuscript_dir}. Skipping.")
        return None

    bib_path = _pick_file(manuscript_dir, _BIB_CANDIDATES, "*.bib")
    tex_path = _pick_file(manuscript_dir, _TEX_CANDIDATES, "*.tex")
    bib = parse_bib(bib_path) if bib_path else {}
    contexts = extract_citation_contexts(tex_path) if tex_path else {}

    vault_index = _vault_hash_index(output_dir)
    pdf_map: Dict[str, str] = {}
    unmatched_pdfs: List[str] = []
    for pdf in pdfs:
        key, method = match_pdf_to_bibkey(pdf, bib, vault_index)
        if key:
            pdf_map[key] = pdf.name
            writeLog("info", logger,
                     f"[Manuscript] PDF {pdf.name} -> {key} (método: {method})")
        else:
            unmatched_pdfs.append(pdf.name)
            writeLog("warning", logger,
                     f"[Manuscript] PDF sin referencia asociada: {pdf.name}")

    cited_keys = set(contexts.keys())
    references: List[Dict] = []
    for key in sorted(set(bib.keys()) | cited_keys | set(pdf_map.keys())):
        record = bib.get(key, {})
        referenced = key in cited_keys
        pdf_name = pdf_map.get(key)
        if not referenced and not pdf_name:
            continue
        references.append({
            "key": key,
            "title": record.get("title") or record.get("booktitle") or key,
            "doi": record.get("doi", ""),
            "cited": referenced,
            "in_bib": key in bib,
            "pdf_file": pdf_name,
            "has_pdf": bool(pdf_name),
            "status": pdf_name and "pdf" or (referenced and "cited_no_pdf" or "bib_only"),
            "contexts": contexts.get(key, []),
        })

    refs = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "manuscript_dir": str(manuscript_dir),
        "bib_file": bib_path.name if bib_path else None,
        "tex_file": tex_path.name if tex_path else None,
        "n_references": len(references),
        "n_cited": len(cited_keys),
        "n_with_pdf": sum(1 for r in references if r["has_pdf"]),
        "unmatched_pdfs": unmatched_pdfs,
        "cited_without_bib": sorted(cited_keys - set(bib.keys())),
        "references": references,
    }

    if persist and output_dir:
        out_file = Path(output_dir) / REFS_FILE_NAME
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(refs, indent=2, ensure_ascii=False), encoding="utf-8")
        writeLog("info", logger, f"[Manuscript] Contrato guardado en {out_file}")

    writeLog("info", logger,
             f"[Manuscript] {refs['n_cited']} citas, {refs['n_with_pdf']} con PDF, "
             f"{len(unmatched_pdfs)} PDFs sin referencia, "
             f"{len(refs['cited_without_bib'])} citas sin entrada bib.")
    return refs


# ---------------------------------------------------------------------------
# Modelo de datos de la evidencia del manuscrito
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ManuscriptReference:
    bibkey: str
    title: str
    doi: str
    paper_id: Optional[str]
    contexts: Tuple[str, ...]
    extension: str
    structured_evidence: Dict = field(default_factory=dict)
    has_pdf: bool = False


@dataclass(frozen=True)
class MissingReference:
    """Referencia citada que no se ha podido enriquecer (susceptible de error)."""

    bibkey: str
    title: str
    doi: str
    contexts: Tuple[str, ...]
    reason: str  # "no_pdf" | "pdf_not_indexed"
    in_bib: bool = True


@dataclass(frozen=True)
class ManuscriptEvidence:
    references: Tuple[ManuscriptReference, ...]
    cited_without_pdf: Tuple[MissingReference, ...]
    unmatched_pdfs: Tuple[str, ...]
    n_cited: int
    n_matched: int


# ---------------------------------------------------------------------------
# Constructor de la evidencia (matching con corpus + extensión de la cita)
# ---------------------------------------------------------------------------

class ManuscriptEvidenceBuilder:
    """Construye ManuscriptEvidence a partir de manuscript_refs.json."""

    _SYSTEM_PROMPT = (
        "Eres un asistente de investigación. Ayudas a un investigador a recordar "
        "por qué citó una referencia en su manuscrito. Usando ÚNICAMENTE la "
        "evidencia proporcionada del paper citado, escribe 1-2 frases en español "
        "que expliquen su contribución principal y el concepto relevante para la "
        "cita. No inventes información. Devuelve SOLO un JSON con la clave "
        "'extension'."
    )

    def __init__(
        self,
        llm_client,
        cache_file: Optional[Path] = None,
        extractor=None,
        max_tokens: int = 300,
    ) -> None:
        self._llm = llm_client
        self._cache_file = Path(cache_file) if cache_file else None
        self._extractor = extractor
        self._max_tokens = max_tokens
        self._cache: Dict[str, str] = {}
        self._dirty = False
        self._load_cache()

    # -- caché ------------------------------------------------------------
    def _load_cache(self) -> None:
        if self._cache_file and self._cache_file.exists():
            try:
                self._cache = json.loads(self._cache_file.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}

    def save_cache(self) -> None:
        if self._cache_file and self._dirty:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(
                json.dumps(self._cache, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    # -- API --------------------------------------------------------------
    def build(
        self,
        refs: Dict,
        papers_text: Dict[str, Dict],
        metadata_lookup: Dict[str, Dict],
    ) -> ManuscriptEvidence:
        doi_to_pid, title_to_pid = self._build_paper_index(papers_text, metadata_lookup)

        matched: List[ManuscriptReference] = []
        cited_without_pdf: List[MissingReference] = []
        n_cited = 0

        for ref in refs.get("references", []):
            key = ref.get("key", "")
            if ref.get("cited"):
                n_cited += 1
            paper_id = self._resolve_paper_id(ref, doi_to_pid, title_to_pid)
            contexts = tuple(ref.get("contexts", []))

            if not paper_id or paper_id not in papers_text:
                if ref.get("cited"):
                    cited_without_pdf.append(MissingReference(
                        bibkey=key,
                        title=ref.get("title", key),
                        doi=ref.get("doi", ""),
                        contexts=contexts,
                        reason="pdf_not_indexed" if ref.get("has_pdf") else "no_pdf",
                        in_bib=bool(ref.get("in_bib", True)),
                    ))
                continue

            paper_ctx = dict(papers_text.get(paper_id) or {})
            meta = metadata_lookup.get(paper_id, {})
            paper_ctx.setdefault("abstract", meta.get("abstract", ""))
            structured = self._extract_structured(paper_id, paper_ctx)
            extension = self._make_extension(
                key, ref.get("title", key), contexts, structured, paper_ctx
            )
            matched.append(ManuscriptReference(
                bibkey=key,
                title=ref.get("title", key),
                doi=ref.get("doi", ""),
                paper_id=paper_id,
                contexts=contexts,
                extension=extension,
                structured_evidence=structured,
                has_pdf=bool(ref.get("has_pdf")),
            ))

        self.save_cache()
        writeLog("info", logger,
                 f"[Manuscript] Evidencia construida: {len(matched)} papers con texto, "
                 f"{len(cited_without_pdf)} citas sin PDF asociado.")
        return ManuscriptEvidence(
            references=tuple(matched),
            cited_without_pdf=tuple(cited_without_pdf),
            unmatched_pdfs=tuple(refs.get("unmatched_pdfs", [])),
            n_cited=n_cited,
            n_matched=len(matched),
        )

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _build_paper_index(
        papers_text: Dict[str, Dict],
        metadata_lookup: Dict[str, Dict],
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        doi_to_pid: Dict[str, str] = {}
        title_to_pid: Dict[str, str] = {}
        for pid, meta in metadata_lookup.items():
            doi = (meta.get("doi") or "").strip().lower()
            if doi:
                doi_to_pid[doi] = pid
            title = (meta.get("title") or "").strip().lower()
            if title:
                title_to_pid[title] = pid
        return doi_to_pid, title_to_pid

    def _resolve_paper_id(
        self,
        ref: Dict,
        doi_to_pid: Dict[str, str],
        title_to_pid: Dict[str, str],
    ) -> Optional[str]:
        doi = (ref.get("doi") or "").strip().lower()
        if doi and doi in doi_to_pid:
            return doi_to_pid[doi]
        title = (ref.get("title") or "").strip().lower()
        if title and title in title_to_pid:
            return title_to_pid[title]
        return None

    def _extract_structured(self, paper_id: str, paper: Optional[Dict]) -> Dict:
        if not self._extractor or not paper:
            return {}
        try:
            return self._extractor.extract(paper_id, paper).to_dict()
        except Exception as exc:  # pragma: no cover
            writeLog("warning", logger,
                     f"[Manuscript] Falló extracción estructurada de {paper_id}: {exc}")
            return {}

    def _make_extension(
        self,
        key: str,
        title: str,
        contexts: Tuple[str, ...],
        structured: Dict,
        paper: Optional[Dict],
    ) -> str:
        cache_key = f"{key}::{sha1('|'.join(contexts)[:500])}" if contexts else f"{key}::noctx"
        if cache_key in self._cache:
            return self._cache[cache_key]

        grounded = self._grounding_text(structured, paper)
        extension = ""
        if self._llm and grounded:
            extension = self._llm_extension(contexts, title, grounded)
        if not extension:
            extension = self._extractive_extension(title, structured, paper)

        if extension:
            self._cache[cache_key] = extension
            self._dirty = True
        return extension

    @staticmethod
    def _grounding_text(structured: Dict, paper: Optional[Dict]) -> str:
        parts: List[str] = []
        if paper:
            abstract = paper.get("abstract", "")
            if abstract:
                parts.append(f"Abstract: {abstract[:1200]}")
        if structured:
            for label, key in (
                ("Contribución principal", "solution"),
                ("Contexto/ideas", "ideas"),
                ("Uso", "usage"),
                ("Métodos", "methods"),
                ("Resultados", "results"),
            ):
                val = structured.get(key)
                if val:
                    parts.append(f"{label}: {str(val)[:800]}")
        return "\n\n".join(parts)[:3000]

    def _llm_extension(self, contexts: Tuple[str, ...], title: str, grounded: str) -> str:
        citations = "\n".join(f"- {c}" for c in contexts[:3]) or "(sin contexto de cita)"
        raw = self._llm.generate_json(
            prompt=(
                f"Contexto(s) donde se cita el paper en el manuscrito:\n{citations}\n\n"
                f"Título del paper citado: {title}\n\n"
                f"Evidencia del paper:\n{grounded}\n\n"
                'Devuelve JSON: {"extension": "..."}'
            ),
            system_prompt=self._SYSTEM_PROMPT,
            context=f"manuscript:{title[:40]}",
            expect_array=False,
            max_tokens=self._max_tokens,
        )
        return re.sub(r"\s+", " ", str(raw.get("extension", ""))).strip()[:600]

    @staticmethod
    def _extractive_extension(title: str, structured: Dict, paper: Optional[Dict] = None) -> str:
        candidates: List[str] = []
        abstract = (paper or {}).get("abstract", "")
        if abstract:
            candidates.append(str(abstract).strip())
        for key in ("solution", "ideas", "applications", "results"):
            val = structured.get(key)
            if val:
                candidates.append(str(val).strip())

        for candidate in candidates:
            sentences = re.split(r"(?<=[.!?])\s+", candidate)
            out = " ".join(sentences[:2]).strip()
            if len(out) >= 40:
                return out[:600]
        return ""
