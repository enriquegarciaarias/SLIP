"""
query_expander.py
=================
Expande queries de conceptos usando un LLM para enriquecer la recuperación
de documentos académicos.

Cambios respecto a la versión anterior:
  - Sin mutación de dicts externos: trabaja con copias (inmutabilidad)
  - Sin print(): todo el logging pasa por writeLog
  - Lógica de selección de prompt centralizada en _resolve_prompt()
  - Config cargada fuera del constructor (SRP); __init__ sólo recibe dicts
  - Tipos explícitos en toda la interfaz pública
  - SIN configuración LLM interna: delegada 100% a llm_client.py
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sources.common.common import logger, writeLog
from sources.common.llm_client import LLMClient, create_resilient_ollama_client


# ---------------------------------------------------------------------------
# Objeto de configuración tipado (evita accesos a dict con .get por doquier)
# NOTA: Las variables de LLM (modelo, reintentos, tokens) se han ELIMINADO.
# Se leen automáticamente desde processControl.defaults.llm vía llm_client.py
# ---------------------------------------------------------------------------

@dataclass
class QueryExpanderConfig:
    enabled: bool = True
    max_expansion_terms: int = 7
    replace_query: bool = False
    # Prompts específicos por concept_id  {"concept_id": "prompt…"}
    concept_specific_prompts: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict) -> "QueryExpanderConfig":
        """Construye la config desde un dict (e.g. JSON cargado por el caller)."""
        return cls(
            enabled=data.get("enabled", True),
            max_expansion_terms=data.get("max_expansion_terms", 7),
            replace_query=data.get("replace_query", False),
            # Ignoramos llm_model o max_retries si existieran en el JSON por compatibilidad hacia atrás
            concept_specific_prompts=data.get("concept_specific_prompts", {}),
        )

    @classmethod
    def from_file(cls, path: str) -> "QueryExpanderConfig":
        """Lee un JSON y construye la config. Lanza FileNotFoundError si no existe."""
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


# ---------------------------------------------------------------------------
# Resultado de expansión (no mutamos el dict original)
# ---------------------------------------------------------------------------

@dataclass
class ExpansionResult:
    concept_id: str
    original_query: str
    expanded_queries: List[str]
    success: bool

    def apply_to(self, concept: Dict) -> Dict:
        """
        Devuelve una COPIA del dict original con los campos de expansión añadidos.
        No muta el dict recibido.
        """
        updated = dict(concept)
        updated["original_query"] = self.original_query
        updated["expanded_queries"] = self.expanded_queries
        return updated


# ---------------------------------------------------------------------------
# Servicio principal
# ---------------------------------------------------------------------------

class QueryExpander:
    """
    Servicio que expande la query de un concepto usando un LLM.

    Recibe su configuración ya construida (no lee ficheros por sí mismo)
    y el cliente LLM por inyección de dependencias. Si no se inyecta,
    usa la factory resilient por defecto (que lee de processControl).
    """

    _SYSTEM_PROMPT = "You are a JSON generator that returns a JSON array of strings."

    def __init__(
        self,
        config: Optional[QueryExpanderConfig] = None,
        llm_client: Optional[LLMClient] = None,
    ) -> None:
        self._config = config or QueryExpanderConfig()
        # Si no se inyecta cliente, se crea uno resiliente usando la config global
        self._llm = llm_client or create_resilient_ollama_client()

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def expand_query(self, concept: Dict) -> ExpansionResult:
        """
        Expande la query de un concepto.
        Devuelve un ExpansionResult; no muta el dict de entrada.
        """
        concept_id = str(concept.get("concept_id", "unknown"))
        original_query = concept.get("concept_query", "") or concept.get("concept_name", "")
        concept_name = concept.get("concept_name", "")
        concept_desc = concept.get("concept_description", "")
        inline_prompt = concept.get("expansion_prompt", "")

        empty_result = ExpansionResult(
            concept_id=concept_id,
            original_query=original_query,
            expanded_queries=[],
            success=False,
        )

        if not self._config.enabled:
            return empty_result

        if not original_query:
            writeLog("warning", logger,
                     f"[QueryExpander] Concept {concept_id}: no query or name, skipping")
            return empty_result

        prompt = self._build_prompt(
            query=original_query,
            name=concept_name,
            desc=concept_desc,
            inline_prompt=inline_prompt,
            concept_id=concept_id,
        )

        terms = self._llm.generate_json_array(
            prompt=prompt,
            system_prompt=self._SYSTEM_PROMPT,
            context=f"concept:{concept_id}",
        )

        if not terms:
            writeLog("warning", logger,
                     f"[QueryExpander] Concept {concept_id}: expansion returned no terms")
            return empty_result

        trimmed = terms[: self._config.max_expansion_terms]
        writeLog(
            "info", logger,
            f"[QueryExpander] Concept {concept_id}: '{original_query}' → {len(trimmed)} terms "
            f"(preview: {trimmed[:3]})",
        )

        return ExpansionResult(
            concept_id=concept_id,
            original_query=original_query,
            expanded_queries=trimmed,
            success=True,
        )

    def expand_all(self, aligned_concepts: Dict) -> Dict:
        """
        Expande todos los conceptos de un dict con estructura
        {"aligned_concepts": [...]} y devuelve una COPIA actualizada.
        """
        if not self._config.enabled:
            writeLog("info", logger, "[QueryExpander] Expansion disabled, skipping")
            return aligned_concepts

        updated_concepts: List[Dict] = []
        for concept in aligned_concepts.get("aligned_concepts", []):
            result = self.expand_query(concept)
            updated_concepts.append(result.apply_to(concept))

        return {**aligned_concepts, "aligned_concepts": updated_concepts}

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _resolve_prompt(self, inline_prompt: str, concept_id: str) -> Optional[str]:
        """
        Determina el prompt específico a usar, si existe.
        Prioridad: inline (en el propio concepto) > config por concept_id > None
        """
        if inline_prompt:
            writeLog("info", logger,
                     f"[QueryExpander] Concept {concept_id}: using inline expansion_prompt")
            return inline_prompt

        config_prompt = self._config.concept_specific_prompts.get(concept_id)
        if config_prompt:
            writeLog("info", logger,
                     f"[QueryExpander] Concept {concept_id}: using config-specific prompt")
            return config_prompt

        return None

    def _build_prompt(
        self,
        query: str,
        name: str,
        desc: str,
        inline_prompt: str,
        concept_id: str,
    ) -> str:
        """Devuelve el prompt final, específico o genérico."""
        specific = self._resolve_prompt(inline_prompt, concept_id)
        if specific:
            return specific

        return (
            f"Generate a JSON array of {self._config.max_expansion_terms} alternative phrases, "
            f"synonyms, and related technical terms that would help retrieve relevant academic papers.\n\n"
            f"Requirements:\n"
            f"- Each term is a short phrase (2-6 words)\n"
            f"- Technical and academic terminology\n"
            f"- Specific, not generic\n\n"
            f'Example: ["term1", "term2", "term3"]\n\n'
            f"Original concept: {query}\n"
            f"Concept name: {name}\n"
            f"Description: {desc}\n\n"
            f"Respond ONLY with the JSON array, nothing else."
        )


# ---------------------------------------------------------------------------
# Helpers de conveniencia para compatibilidad con código existente
# ---------------------------------------------------------------------------

def load_config(config_dict: Optional[Dict] = None, config_file: Optional[str] = None) -> QueryExpanderConfig:
    """
    Carga la config desde un dict o fichero. El caller decide cuándo leer disco.
    Si se pasan ambos, el fichero tiene precedencia sobre el dict.
    """
    if config_file and os.path.exists(config_file):
        return QueryExpanderConfig.from_file(config_file)
    if config_dict:
        return QueryExpanderConfig.from_dict(config_dict)
    return QueryExpanderConfig()


def expand_all_concepts(
    aligned_concepts: Dict,
    config: Optional[Dict] = None,
    config_file: Optional[str] = None,
) -> Dict:
    """
    Función de conveniencia que mantiene la firma original para
    compatibilidad con código existente.
    """
    cfg = load_config(config, config_file)
    expander = QueryExpander(config=cfg)
    return expander.expand_all(aligned_concepts)