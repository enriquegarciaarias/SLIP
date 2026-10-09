"""
generate_technical_annex.py
============================
Extracción de perfiles técnicos de papers académicos mediante LLM.

El módulo es genérico: los campos a extraer se definen en studyDescription.json,
por lo que sirve para cualquier tipo de investigación sin modificar código.

Arquitectura en capas (SOA):
  ┌─────────────────────────────────────────────────┐
  │       TechnicalAnnexPipeline  (orquestador)     │  ← punto de entrada
  ├──────────────────┬──────────────────────────────┤
  │   AnnexConfig    │  PaperSelector               │
  │   (config)       │  (filtrado por evidencias)   │
  ├──────────────────┴──────────────────────────────┤
  │            TechnicalProfileExtractor            │
  │   (selección de sección + prompt + LLM call)    │
  ├─────────────────────────────────────────────────┤
  │            LLMClient  (federado, externo)        │
  └─────────────────────────────────────────────────┘

Principios aplicados:
  - SRP  : cada clase tiene una única razón para cambiar
  - OCP  : campos de extracción definidos en JSON, sin tocar código
  - DIP  : LLMClient inyectado, no instanciado aquí
  - Sin constantes globales mutables
  - Sin reimplementación de HTTP/reintentos/JSON (delega en llm_client)
  - Sin time.sleep acoplado al procesamiento (política de throttle separada)
  - El modelo LLM se gestiona 100% en llm_client.py (processControl)
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from tqdm import tqdm

from sources.common.common import logger, writeLog
from sources.common.llm_client import LLMClient, create_resilient_ollama_client
from sources.common.utils import inicioModulo


# ---------------------------------------------------------------------------
# Objetos de configuración tipados
# ---------------------------------------------------------------------------

@dataclass
class ExtractionField:
    """
    Define un campo a extraer del paper.

    `description` es una INSTRUCCIÓN de qué buscar en el paper, nunca un valor
    de ejemplo: el prompt la usa como guía y los modelos pequeños tienden a
    copiarla si contiene la respuesta. Los valores típicos/admitidos van en
    `examples`, que el prompt presenta aparte como vocabulario (hints), jamás
    como salida esperada.
    """
    name: str
    description: str
    type: str = "string"        # "string" | "list"
    examples: List[str] = field(default_factory=list)
    # Mapa sinonimo -> etiqueta canónica (p. ej. inglés -> español). Se aplica
    # de forma determinista TRAS la extracción; nunca se inyecta en el prompt.
    aliases: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict) -> "ExtractionField":
        raw_examples = data.get("examples") or []
        if isinstance(raw_examples, str):
            raw_examples = [raw_examples]
        raw_aliases = data.get("aliases") or {}
        if not isinstance(raw_aliases, dict):
            raw_aliases = {}
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            type=data.get("type", "string"),
            examples=[str(e) for e in raw_examples],
            aliases={str(k): str(v) for k, v in raw_aliases.items()},
        )

    @property
    def is_list(self) -> bool:
        return self.type == "list"


# Valores centinela: salidas legítimas de "no hay nada" que nunca deben
# contarse como eco de la lista de ejemplos.
_SENTINEL_ECHO_VALUES = {
    "no evaluada", "no evaluado", "no evaluadas", "no evaluados",
    "no aplicado", "no aplicada", "no aplicable", "no se evalua",
    "no reportado", "no reportada", "no especificado", "no especificada",
    "not evaluated", "not applicable", "not reported", "not assessed",
    "none", "ninguno", "ninguna", "na", "n a", "unknown", "desconocido",
}


_SPANISH_MARKS = set("áéíóúüñ")


def _normalize_for_compare(text) -> str:
    """Minúsculas, sin acentos ni signos, espacios colapsados."""
    text = unicodedata.normalize("NFKD", str(text).lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_discriminative_example(raw_example) -> bool:
    """
    Un ejemplo es "discriminativo de idioma" si tiene marca (acento/ñ) o varias
    palabras. Los tokens neutros (EEG, AUC, WESAD, DEAP, ECG...) NO son
    discriminativos: pueden ser extracciones legítimas y no deben disparar el
    eco por sí solos.
    """
    raw = str(raw_example)
    if any(ch.lower() in _SPANISH_MARKS for ch in raw):
        return True
    return bool(re.search(r"\s", raw.strip()))


def classify_echo(value, field: "ExtractionField") -> tuple:
    """
    Clasifica si el valor extraído es un eco de la descripción/ejemplos.

    Devuelve ``(kind, cleaned_value)`` con ``kind`` en:
      - ``"none"``    : valor legítimo (se deja igual).
      - ``"full"``    : eco puro -> el campo se resetea (``[]`` / ``None``).
      - ``"partial"`` : eco mixto -> ``cleaned_value`` conserva los items que
                        NO son ejemplos discriminativos.

    Reglas (conservadoras, solo cuentan ejemplos discriminativos):
      * eco de la descripción (como antes);
      * conjunto exactamente igual al vocabulario completo de ejemplos;
      * subconjunto puro: ningún item propio y >=2 ejemplos discriminativos;
      * mixto: >=2 ejemplos discriminativos y algún item propio -> poda;
      * colapso en un único string con >=2 ejemplos discriminativos.
    Los centinelas ("no evaluada", ...) no cuentan como coincidencia.
    """
    is_list = isinstance(value, list)
    raw_items = list(value) if is_list else [value]
    norm_items = [_normalize_for_compare(v) for v in raw_items]
    norm_items = [n for n in norm_items if n]
    reset_value = [] if is_list else None

    # 1) Eco de la descripción.
    desc = _normalize_for_compare(field.description)
    for n in norm_items:
        if desc and n == desc:
            return "full", reset_value
        if len(n) >= 40 and desc and (n in desc or desc in n):
            return "full", reset_value

    if not (field.is_list and field.examples):
        return "none", value

    example_norms = [_normalize_for_compare(e) for e in field.examples]
    example_norms = [e for e in example_norms if e]
    all_examples = set(example_norms)
    substantive = set(example_norms) - _SENTINEL_ECHO_VALUES

    # Solo los ejemplos multi-palabra o con diacríticos pueden delatar el eco.
    disc_norms = {
        _normalize_for_compare(raw)
        for raw in field.examples
        if _is_discriminative_example(raw)
    }
    disc_norms = {e for e in disc_norms if e and e in substantive}
    if not disc_norms:
        return "none", value

    # 2) Conjunto exactamente igual al vocabulario completo de ejemplos.
    if set(norm_items) == all_examples:
        return "full", reset_value

    # 3) Colapso en un único string con TODOS los ejemplos discriminativos
    #    (solo si hay >=2, para no borrar una única categoría legítima).
    joined = " | ".join(norm_items)
    disc_hits = [e for e in disc_norms if e in joined]
    if len(disc_norms) >= 2 and len(norm_items) == 1 and len(disc_hits) == len(disc_norms):
        return "full", reset_value

    if is_list:
        substantive_items = [n for n in norm_items if n not in _SENTINEL_ECHO_VALUES]
        matched = [n for n in substantive_items if n in disc_norms]
        genuine = [n for n in substantive_items if n not in all_examples]

        # 4) Subconjunto puro: ningún item propio y >=2 ejemplos discriminativos.
        if len(matched) >= 2 and not genuine:
            return "full", reset_value
        # 5) Eco mixto: podar SOLO los ejemplos discriminativos; conservar
        #    los tokens neutros (EEG, AUC) y los valores propios del paper.
        if len(matched) >= 2 and genuine:
            cleaned = [
                raw for raw, n in zip(raw_items, norm_items) if n not in disc_norms
            ]
            return "partial", cleaned
        # 6) Colapso parcial: un único string con >=2 ejemplos embebidos.
        if len(norm_items) == 1 and len(disc_hits) >= 2:
            return "full", reset_value
    else:
        # 7) String único que embebe >=2 ejemplos discriminativos.
        if len(disc_hits) >= 2:
            return "full", reset_value

    return "none", value


def canonicalize_value(value, field: "ExtractionField"):
    """
    Canonicaliza deterministamente la salida del LLM usando `field.examples`
    como etiquetas canónicas y `field.aliases` como mapa sinónimo -> etiqueta.

    El vocabulario NO se envía al prompt (Capa 2): el modelo extrae texto libre
    en el idioma del paper y aquí se normaliza. Los ítems sin coincidencia se
    conservan tal cual (texto libre).
    """
    if not field.examples:
        return value

    # Etiquetas canónicas (match exacto) y alias (exacto + contención).
    labels_norm: Dict[str, str] = {}
    for label in field.examples:
        labels_norm.setdefault(_normalize_for_compare(label), label)

    exact_aliases: Dict[str, str] = {}
    partial_aliases = []  # (clave_normalizada, etiqueta)
    for alias, label in (field.aliases or {}).items():
        key = _normalize_for_compare(alias)
        if not key:
            continue
        exact_aliases[key] = label
        if len(key) >= 4:
            partial_aliases.append((key, label))
    partial_aliases.sort(key=lambda kv: len(kv[0]), reverse=True)

    def _canon(item):
        n = _normalize_for_compare(item)
        if not n:
            return item
        if n in labels_norm:
            return labels_norm[n]
        if n in exact_aliases:
            return exact_aliases[n]
        for key, label in partial_aliases:
            if re.search(rf"\b{re.escape(key)}\b", n):
                return label
        return item

    if isinstance(value, list):
        seen, result = set(), []
        for item in value:
            mapped = _canon(item)
            key = _normalize_for_compare(mapped)
            if key and key not in seen:
                seen.add(key)
                result.append(mapped)
        return result
    if isinstance(value, str):
        return _canon(value)
    return value


@dataclass
class AnnexConfig:
    """
    Configuración completa del módulo.
    Se construye desde studyDescription.json; no hay constantes globales.

    NOTA: 'llm_model' se ha eliminado. Se usa el modelo primario de
    processControl.defaults.llm vía create_resilient_ollama_client().
    Se mantienen 'temperature' y 'max_tokens' porque son hiperparámetros
    específicos de la extracción de JSON estructurado (necesitan ser más
    estrictos que la generación de texto libre).
    """
    temperature: float = 0.1           # Baja para extracción estable de JSON
    max_section_chars: int = 3000
    max_tokens: int = 4096             # JSON grande requiere más espacio
    throttle_seconds: float = 1.0      # pausa entre llamadas al LLM
    extraction_fields: List[ExtractionField] = field(default_factory=list)

    @classmethod
    def from_project_config(cls, project: Dict) -> "AnnexConfig":
        """Construye la config desde el bloque 'project' de studyDescription.json."""
        llm_cfg = project.get("llm_config", {})
        annex_cfg = project.get("technical_extraction", {})

        raw_fields = annex_cfg.get("fields", [])
        extraction_fields = [ExtractionField.from_dict(f) for f in raw_fields]

        return cls(
            temperature=llm_cfg.get("temperature", 0.1),
            max_section_chars=annex_cfg.get("max_section_chars", 3000),
            max_tokens=llm_cfg.get("max_tokens", 4096),
            throttle_seconds=annex_cfg.get("throttle_seconds", 1.0),
            extraction_fields=extraction_fields,
        )

    @classmethod
    def from_file(cls, study_file: Path) -> "AnnexConfig":
        if not study_file.exists():
            writeLog("error", logger,
                     f"[AnnexConfig] studyDescription.json not found: {study_file}")
            return cls()
        with open(study_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_project_config(data.get("project", {}))


# ---------------------------------------------------------------------------
# Resultado de dominio
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TechnicalProfile:
    """Perfil técnico de un paper: inmutable una vez extraído."""
    paper_id: str
    title: str
    profile: Dict         # campos extraídos por el LLM

    @property
    def is_empty(self) -> bool:
        return not self.profile

    def to_dict(self) -> Dict:
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "profile": self.profile,
        }


# ---------------------------------------------------------------------------
# Capa 1 – Selección de papers por evidencias (filtrado)
# ---------------------------------------------------------------------------

class PaperSelector:
    """
    Determina qué papers procesar para cada concepto.
    Estrategia: si hay concept_findings, usar sólo papers con evidencia;
    si no, usar todos los alineados al concepto.
    """

    def __init__(self, findings_data: Optional[List[Dict]] = None) -> None:
        # concept_id -> lista ordenada de paper_ids con evidencia
        self._evidence_index: Dict = self._build_index(findings_data or [])

    @staticmethod
    def _build_index(findings: List[Dict]) -> Dict[str, List[str]]:
        index: Dict[str, List[str]] = {}
        for concept in findings:
            cid = concept.get("concept_id")
            if cid is not None:
                index[str(cid)] = concept.get("all_papers", [])
        return index

    def select_for_concept(
        self, concept_id: str, aligned_doc_ids: List[str]
    ) -> List[str]:
        """
        Devuelve la lista ordenada de paper_ids a procesar para un concepto.
        Si hay filtro de evidencias: papers con evidencia primero, resto después.
        Si no hay filtro: todos los alineados.
        """
        aligned_set = set(aligned_doc_ids)

        if concept_id not in self._evidence_index:
            writeLog("info", logger,
                     f"[PaperSelector] Concept {concept_id}: "
                     f"no evidence filter, using {len(aligned_doc_ids)} aligned papers")
            return list(aligned_doc_ids)

        evidence_ids = self._evidence_index[concept_id]
        # Evidencias primero (en su orden original), luego el resto
        ordered = [pid for pid in evidence_ids if pid in aligned_set]
        remainder = [pid for pid in aligned_doc_ids if pid not in set(ordered)]
        result = ordered + remainder

        writeLog("info", logger,
                 f"[PaperSelector] Concept {concept_id}: "
                 f"{len(ordered)} with evidence + {len(remainder)} others = {len(result)} total")
        return result

    def all_paper_ids(self, concept_doc_map: Dict[str, List[str]]) -> Set[str]:
        """Devuelve el conjunto único de todos los paper_ids a procesar."""
        return {pid for ids in concept_doc_map.values() for pid in ids}

    @property
    def has_evidence_filter(self) -> bool:
        return bool(self._evidence_index)


# ---------------------------------------------------------------------------
# Capa 2 – Extracción de perfil técnico (prompt + LLM)
# ---------------------------------------------------------------------------

class TechnicalProfileExtractor:
    """
    Responsabilidad única: dado un paper y una lista de campos,
    construye el prompt, llama al LLM y devuelve el perfil extraído.
    No hace I/O de ficheros.
    """

    # Secciones del paper en orden de preferencia
    _BACKGROUND_KEYS = ("abstract", "introduction")
    _METHODOLOGY_KEYS = ("methodology", "methods", "approach", "materials")
    _RESULTS_KEYS = ("results", "experiments", "evaluation", "discussion")

    _SYSTEM_PROMPT = (
        "You are a structured data extractor. "
        "You extract technical details from academic papers and return only valid JSON."
    )

    def __init__(self, llm_client: LLMClient, config: AnnexConfig) -> None:
        self._llm = llm_client
        self._config = config

    def extract(self, paper: Dict, paper_id: str, title: str) -> TechnicalProfile:
        """
        Extrae el perfil técnico de un paper.
        Devuelve siempre un TechnicalProfile (vacío si el LLM falla).
        """
        if not self._config.extraction_fields:
            writeLog("warning", logger,
                     f"[Extractor] No extraction fields configured for {paper_id}")
            return TechnicalProfile(paper_id=paper_id, title=title, profile={})

        background, methodology, results = self._select_sections(paper)
        prompt = self._build_prompt(title, background, methodology, results)

        # Se inyectan max_tokens y temperature específicos de extracción técnica
        raw_profile = self._llm.generate_json(
            prompt=prompt,
            system_prompt=self._SYSTEM_PROMPT,
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
            context=f"tech-annex:{paper_id}",
            expect_array=False,
        )

        profile = self._coerce_field_types(raw_profile)
        profile = self._canonicalize_fields(profile)
        profile = self._drop_echoed_fields(profile)
        return TechnicalProfile(paper_id=paper_id, title=title, profile=profile)

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _select_sections(self, paper: Dict) -> tuple[str, str, str]:
        """
        Extrae las secciones de contexto (abstract/introducción), metodología y
        resultados del paper. Fallback al texto completo si no hay secciones.
        """
        # papers_text.json expone las secciones como `clean_sections`; se admite
        # `sections` por compatibilidad con versiones antiguas del contrato.
        sections = paper.get("clean_sections") or paper.get("sections", {})
        limit = self._config.max_section_chars

        background = " ".join(
            sections[k] for k in self._BACKGROUND_KEYS if sections.get(k)
        )
        methodology = next(
            (sections[k] for k in self._METHODOLOGY_KEYS if sections.get(k)), ""
        )
        results = next(
            (sections[k] for k in self._RESULTS_KEYS if sections.get(k)), ""
        )

        if not methodology and not results:
            full = paper.get("full_text", "") or paper.get("clean_text", "")
            methodology = full[:limit]

        return background[:limit], methodology[:limit], results[:limit]

    def _build_prompt(
        self, title: str, background: str, methodology: str, results: str
    ) -> str:
        """
        Construye el prompt dinámicamente a partir de los campos configurados.

        El esquema usa placeholders neutros y las descripciones se presentan
        como guía aparte, con la prohibición explícita de copiarlas. Así se evita
        el fallo observado: modelos pequeños devolvían la descripción del campo
        (o sus valores de ejemplo) como si fuera el valor extraído.
        """
        schema_lines, guidance_lines = [], []
        for f in self._config.extraction_fields:
            if f.is_list:
                schema_lines.append(f'  "{f.name}": ["<string>", ...]')
                kind = "list of strings"
            else:
                schema_lines.append(f'  "{f.name}": "<string or null>"')
                kind = "string"
            # Capa 2: NO se inyectan los `examples` (vocabulario canónico) en el
            # prompt; el modelo extrae texto libre y se canonicaliza después.
            guidance_lines.append(f"- {f.name} ({kind}): {f.description}")

        json_schema = "{\n" + ",\n".join(schema_lines) + "\n}"
        fields_guidance = "\n".join(guidance_lines)

        return (
            "Extract the following fields from this academic paper and return ONLY a valid JSON object.\n\n"
            f"**Title:** {title}\n\n"
            f"**Background (Abstract / Introduction):**\n{background}\n\n"
            f"**Methodology / Materials & Methods:**\n{methodology}\n\n"
            f"**Results / Experiments:**\n{results}\n\n"
            f"**Required JSON structure (keys and types only):**\n{json_schema}\n\n"
            f"**Field guidance (what each field means; DO NOT copy this text):**\n"
            f"{fields_guidance}\n\n"
            f"Rules:\n"
            f"- Base every value ONLY on the paper text above. Do not use outside knowledge.\n"
            f"- NEVER output the field guidance/description text as a value.\n"
            f"- Use null for missing string fields, [] for missing list fields.\n"
            f"- For list fields, include an item only if the paper explicitly supports it.\n"
            f"- Keep values in the language of the paper.\n"
            f"- Output the JSON object only. No explanation, no markdown fences.\n\n"
            f"JSON:"
        )

    def _coerce_field_types(self, profile: Dict) -> Dict:
        """
        Garantiza que los campos de tipo 'list' sean listas
        aunque el LLM haya devuelto otro tipo.
        """
        for f in self._config.extraction_fields:
            if f.is_list and f.name in profile:
                if not isinstance(profile[f.name], list):
                    writeLog("warning", logger,
                             f"[Extractor] Field '{f.name}' expected list, got "
                             f"{type(profile[f.name]).__name__}; resetting to []")
                    profile[f.name] = []
        return profile

    def _canonicalize_fields(self, profile: Dict) -> Dict:
        """
        Canonicaliza deterministamente los valores usando los `examples` como
        etiquetas y `aliases` como sinónimos. No toca la red (Capa 2).
        """
        for f in self._config.extraction_fields:
            if f.name in profile and profile[f.name] is not None:
                profile[f.name] = canonicalize_value(profile[f.name], f)
        return profile

    # ------------------------------------------------------------------
    # Salvaguarda anti-eco
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(text: str) -> str:
        """Compatibilidad: delega en el normalizador del módulo."""
        return _normalize_for_compare(text)

    def _is_echo(self, value, f: ExtractionField) -> bool:
        """True si el valor devuelto es un eco de la descripción o ejemplos."""
        kind, _ = classify_echo(value, f)
        return kind != "none"

    def _drop_echoed_fields(self, profile: Dict) -> Dict:
        """
        Resetea/poda campos cuyo valor es un eco de su descripción/ejemplos.

        Red de seguridad frente a modelos que, pese a las reglas del prompt,
        devuelven el vocabulario de ejemplos como valor extraído. Detecta eco
        puro (reset) y eco mixto (poda de los items que son ejemplos textuales).
        """
        for f in self._config.extraction_fields:
            if f.name not in profile:
                continue
            kind, cleaned = classify_echo(profile[f.name], f)
            if kind == "full":
                writeLog("warning", logger,
                         f"[Extractor] Echo detected in '{f.name}': value repeats "
                         f"the field description/examples; resetting")
                profile[f.name] = [] if f.is_list else None
            elif kind == "partial":
                writeLog("warning", logger,
                         f"[Extractor] Partial echo in '{f.name}': pruning "
                         f"example items, keeping extracted specifics")
                profile[f.name] = cleaned
        return profile


# ---------------------------------------------------------------------------
# Capa 3 – Orquestador del pipeline
# ---------------------------------------------------------------------------

class TechnicalAnnexPipeline:
    """
    Orquesta la carga de datos, el procesamiento de papers y la escritura
    del resultado. No contiene lógica de extracción ni de filtrado.
    """

    def __init__(
        self,
        config: AnnexConfig,
        extractor: TechnicalProfileExtractor,
        selector: PaperSelector,
    ) -> None:
        self._config = config
        self._extractor = extractor
        self._selector = selector

    def run(self, output_dir: Path) -> Optional[Dict]:
        """
        Ejecuta el pipeline completo. Devuelve el dict de resultado o None si falla.
        """
        # --- 1. Cargar inputs ---
        aligned_data = self._load_required(output_dir / "aligned_concepts.json")
        papers_text = self._load_required(output_dir / "papers_text.json")
        if aligned_data is None or papers_text is None:
            return None

        papers_metadata = self._load_optional(output_dir / "papers_metadata.json", [])

        # --- 2. Construir lookups ---
        text_lookup: Dict[str, Dict] = {
            p["paper_id"]: p for p in papers_text if "paper_id" in p
        }
        title_lookup: Dict[str, str] = {
            p["paper_id"]: p.get("title", "Unknown Title")
            for p in papers_metadata if "paper_id" in p
        }

        # --- 3. Determinar qué papers procesar por concepto ---
        concept_doc_map: Dict[str, List[str]] = {}
        for concept in aligned_data.get("aligned_concepts", []):
            cid = str(concept["concept_id"])
            doc_ids = [d.get("doc_id") for d in concept.get("documents", []) if d.get("doc_id")]
            concept_doc_map[cid] = self._selector.select_for_concept(cid, doc_ids)

        all_ids = self._selector.all_paper_ids(concept_doc_map)
        writeLog("info", logger,
                 f"[TechnicalAnnex] Unique papers to process: {len(all_ids)}")

        # --- 4. Extraer perfiles (con barra de progreso tqdm) ---
        profiles = self._process_papers(all_ids, text_lookup, title_lookup)

        # --- 5. Ensamblar resultado por concepto ---
        concepts_with_tech = self._assemble_concepts(
            aligned_data.get("aligned_concepts", []),
            concept_doc_map,
            profiles,
            title_lookup,
        )

        # --- 6. Guardar ---
        output = {
            "schema_version": "1.1",
            "filtered_by_evidence": self._selector.has_evidence_filter,
            "n_concepts": len(concepts_with_tech),
            "concepts": concepts_with_tech,
        }
        output_file = output_dir / "technical_annex.json"
        self._save(output, output_file)
        return output

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _process_papers(
        self,
        paper_ids: Set[str],
        text_lookup: Dict[str, Dict],
        title_lookup: Dict[str, str],
    ) -> Dict[str, TechnicalProfile]:
        """Procesa cada paper usando tqdm para mostrar progreso sin logs por ítem."""
        profiles: Dict[str, TechnicalProfile] = {}
        total = len(paper_ids)

        # Barra de progreso silenciosa: sin descripción larga, solo el contador
        with tqdm(total=total, desc="Extrayendo perfiles técnicos", unit="paper") as pbar:
            for paper_id in paper_ids:
                paper = text_lookup.get(paper_id)
                if not paper:
                    writeLog("warning", logger,
                             f"[TechnicalAnnex] Paper {paper_id} not found in papers_text.json")
                    profiles[paper_id] = TechnicalProfile(
                        paper_id=paper_id, title="Not found", profile={}
                    )
                    pbar.update(1)
                    continue

                title = title_lookup.get(paper_id, f"Untitled ({paper_id})")

                # Extracción (sin log por paper)
                profiles[paper_id] = self._extractor.extract(paper, paper_id, title)

                # Pausa entre llamadas
                if pbar.n < total and self._config.throttle_seconds > 0:
                    time.sleep(self._config.throttle_seconds)

                pbar.update(1)

        writeLog("info", logger,
                 f"[TechnicalAnnex] Processed {total} papers. "
                 f"Empty profiles: {sum(1 for p in profiles.values() if p.is_empty)}")
        return profiles

    @staticmethod
    def _assemble_concepts(
        aligned_concepts: List[Dict],
        concept_doc_map: Dict[str, List[str]],
        profiles: Dict[str, TechnicalProfile],
        title_lookup: Dict[str, str],
    ) -> List[Dict]:
        """Agrupa los perfiles extraídos bajo su concepto correspondiente."""
        result = []
        for concept in aligned_concepts:
            cid = str(concept["concept_id"])
            doc_ids = concept_doc_map.get(cid, [])

            papers = []
            for doc_id in doc_ids:
                if doc_id in profiles:
                    papers.append(profiles[doc_id].to_dict())
                else:
                    papers.append({
                        "paper_id": doc_id,
                        "title": title_lookup.get(doc_id, "Unknown"),
                        "profile": {},
                    })

            result.append({
                "concept_id": concept["concept_id"],
                "concept_name": concept.get("concept_name", ""),
                "concept_query": concept.get("concept_query", ""),
                "papers": papers,
            })
        return result

    @staticmethod
    def _load_required(path: Path) -> Optional[Dict | List]:
        if not path.exists():
            writeLog("error", logger, f"[TechnicalAnnex] Required file not found: {path}")
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _load_optional(path: Path, default):
        if not path.exists():
            return default
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _save(data: Dict, path: Path) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        writeLog("info", logger, f"[TechnicalAnnex] Saved to {path}")


# ---------------------------------------------------------------------------
# Factory – construye el pipeline con todas sus dependencias
# ---------------------------------------------------------------------------

def build_pipeline(
    input_dir: Path,
    llm_client: Optional[LLMClient] = None,
    findings_data: Optional[List[Dict]] = None,
) -> TechnicalAnnexPipeline:
    """
    Factory que ensambla el pipeline con sus dependencias inyectadas.
    Separa la construcción del grafo de objetos de la lógica de negocio.

    Args:
        input_dir:     Directorio con studyDescription.json.
        llm_client:    Cliente LLM (se crea uno resiliente por defecto si no se inyecta).
        findings_data: Datos de concept_findings.json (opcional).
    """
    config = AnnexConfig.from_file(input_dir / "studyDescription.json")

    if not config.extraction_fields:
        writeLog("error", logger,
                 "[TechnicalAnnex] No extraction fields in studyDescription.json. Aborting.")
        raise ValueError("No technical_extraction.fields defined in studyDescription.json")

    writeLog("info", logger,
             f"[TechnicalAnnex] {len(config.extraction_fields)} extraction fields loaded. "
             f"Max tokens: {config.max_tokens}, Temp: {config.temperature}")

    # Se usa la factory resiliente. El modelo (y su fallback) se leen de processControl
    client = llm_client or create_resilient_ollama_client()

    extractor = TechnicalProfileExtractor(llm_client=client, config=config)
    selector = PaperSelector(findings_data=findings_data)

    return TechnicalAnnexPipeline(config=config, extractor=extractor, selector=selector)


# ---------------------------------------------------------------------------
# Auditoría anti-eco (dry-run, sin modificar nada)
# ---------------------------------------------------------------------------

def audit_echoes(input_dir: Path, output_dir: Path) -> Dict:
    """
    Informe *dry-run* de ecos en un `technical_annex.json` ya generado.

    No modifica nada: clasifica cada campo de cada perfil con `classify_echo`
    e imprime los sospechosos para revisión manual antes de aplicar los
    reseteos/podas.
    """
    config = AnnexConfig.from_file(Path(input_dir) / "studyDescription.json")
    fields = {f.name: f for f in config.extraction_fields}

    annex_path = Path(output_dir) / "technical_annex.json"
    if not annex_path.exists():
        writeLog("error", logger, f"[EchoAudit] No existe {annex_path}")
        return {}

    with open(annex_path, "r", encoding="utf-8") as fh:
        annex = json.load(fh)

    seen_papers = {}
    findings = []  # (paper_id, title, field, kind, value)
    for concept in annex.get("concepts", []):
        for paper in concept.get("papers", []):
            pid = paper.get("paper_id")
            if pid in seen_papers:
                continue
            seen_papers[pid] = paper.get("title", "")
            profile = paper.get("profile", {}) or {}
            for name, value in profile.items():
                field = fields.get(name)
                if field is None:
                    continue
                kind, cleaned = classify_echo(value, field)
                if kind != "none":
                    findings.append({
                        "paper_id": pid,
                        "title": paper.get("title", ""),
                        "field": name,
                        "kind": kind,
                        "value": value,
                        "cleaned": cleaned,
                    })

    by_field: Dict[str, Dict[str, int]] = {}
    for f in findings:
        bucket = by_field.setdefault(f["field"], {"full": 0, "partial": 0})
        bucket[f["kind"]] += 1

    print("\n" + "=" * 72)
    print(f"AUDITORÍA ANTI-ECO  (dry-run) · {len(seen_papers)} papers únicos")
    print("=" * 72)
    if not findings:
        print("Sin ecos detectados.")
    for name, counts in sorted(by_field.items()):
        print(f"  {name}: {counts['full']} reset · {counts['partial']} poda parcial")
    print("-" * 72)
    for f in findings:
        action = "RESET" if f["kind"] == "full" else "PODA "
        print(f"[{action}] {f['field']}  ({f['paper_id']})")
        print(f"         {f['title'][:80]}")
        print(f"         valor: {f['value']}")
        if f["kind"] == "partial":
            print(f"         conserva: {f['cleaned']}")
    print("=" * 72 + "\n")

    report = {
        "papers": len(seen_papers),
        "n_findings": len(findings),
        "by_field": by_field,
        "findings": findings,
    }
    return report


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def processTechnicalAnnex() -> Optional[Dict]:
    """
    Punto de entrada del módulo.
    Mantiene la misma firma que la versión anterior para compatibilidad.
    """
    input_dir, output_dir = inicioModulo("processGenerateTechnicalAnnex")

    # Cargar findings (opcional) antes de construir el pipeline
    findings_file = output_dir / "concept_findings.json"
    findings_data: Optional[List[Dict]] = None
    if findings_file.exists():
        try:
            with open(findings_file, "r", encoding="utf-8") as fh:
                findings_data = json.load(fh)
            writeLog("info", logger,
                     "[TechnicalAnnex] Loaded concept_findings.json for paper filtering.")
        except Exception as exc:
            writeLog("warning", logger,
                     f"[TechnicalAnnex] Could not load concept_findings.json: {exc}")

    try:
        pipeline = build_pipeline(
            input_dir=input_dir,
            findings_data=findings_data,
        )
    except ValueError:
        return None

    return pipeline.run(output_dir)


if __name__ == "__main__":
    processTechnicalAnnex()