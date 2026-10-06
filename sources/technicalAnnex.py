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
import time
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
    """Define un campo a extraer del paper."""
    name: str
    description: str
    type: str = "string"        # "string" | "list"

    @classmethod
    def from_dict(cls, data: Dict) -> "ExtractionField":
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            type=data.get("type", "string"),
        )

    @property
    def is_list(self) -> bool:
        return self.type == "list"


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

        methodology, results = self._select_sections(paper)
        prompt = self._build_prompt(title, methodology, results)

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
        return TechnicalProfile(paper_id=paper_id, title=title, profile=profile)

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _select_sections(self, paper: Dict) -> tuple[str, str]:
        """
        Extrae las secciones de metodología y resultados del paper.
        Fallback al texto completo si no hay secciones estructuradas.
        """
        # papers_text.json expone las secciones como `clean_sections`; se admite
        # `sections` por compatibilidad con versiones antiguas del contrato.
        sections = paper.get("clean_sections") or paper.get("sections", {})
        limit = self._config.max_section_chars

        methodology = next(
            (sections[k] for k in self._METHODOLOGY_KEYS if sections.get(k)), ""
        )
        results = next(
            (sections[k] for k in self._RESULTS_KEYS if sections.get(k)), ""
        )

        if not methodology and not results:
            full = paper.get("full_text", "") or paper.get("clean_text", "")
            methodology = full[:limit]

        return methodology[:limit], results[:limit]

    def _build_prompt(self, title: str, methodology: str, results: str) -> str:
        """Construye el prompt dinámicamente a partir de los campos configurados."""
        schema_lines = []
        for f in self._config.extraction_fields:
            if f.is_list:
                schema_lines.append(
                    f'  "{f.name}": ["list of strings describing {f.description}"]'
                )
            else:
                schema_lines.append(
                    f'  "{f.name}": "string describing {f.description}"'
                )
        json_schema = "{\n" + ",\n".join(schema_lines) + "\n}"

        return (
            f"Extract the following fields from this academic paper and return ONLY a valid JSON object.\n\n"
            f"**Title:** {title}\n\n"
            f"**Methodology / Materials & Methods:**\n{methodology}\n\n"
            f"**Results / Experiments:**\n{results}\n\n"
            f"**Required JSON structure:**\n{json_schema}\n\n"
            f"Rules:\n"
            f"- Use null for missing string fields, [] for missing list fields.\n"
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