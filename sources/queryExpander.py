# query_expander.py
import json
import os
import re
from typing import List, Dict, Optional
import ollama
import logging

logger = logging.getLogger(__name__)


class QueryExpander:
    """
    Expande queries usando Ollama (con biblioteca oficial)

    La expansión puede venir de dos lugares (por orden de prioridad):
    1. expansion_prompt dentro del propio concepto (desde conceptsQuery.json)
    2. concept_specific_prompts en el archivo de configuración
    3. Prompt genérico basado en query + nombre + descripción
    """

    def __init__(self, config: Dict = None, config_file: str = None):
        self.config = config or {}

        # Cargar configuración desde archivo si existe
        if config_file and os.path.exists(config_file):
            with open(config_file, 'r', encoding='utf-8') as f:
                file_config = json.load(f)
                self.config.update(file_config)

        self.model = self.config.get("llm_model", "llama3.2:3b")
        self.enabled = self.config.get("enabled", True)
        self.max_terms = self.config.get("max_expansion_terms", 7)
        self.replace_query = self.config.get("replace_query", False)
        self.concept_specific_prompts = self.config.get("concept_specific_prompts", {})

    def expand_query(self, concept: Dict) -> Dict:
        """Expande la query de un concepto con términos relacionados."""
        concept_id = str(concept.get("concept_id"))
        original_query = concept.get("concept_query", "")
        concept_name = concept.get("concept_name", "")
        concept_desc = concept.get("concept_description", "")

        # Leer expansion_prompt directamente del concepto
        concept_expansion_prompt = concept.get("expansion_prompt", "")

        if not self.enabled:
            concept["expanded_queries"] = []
            return concept

        if not original_query:
            original_query = concept_name

        if not original_query:
            concept["expanded_queries"] = []
            return concept

        # Prioridad: 1. expansion_prompt del concepto, 2. config prompts, 3. genérico
        if concept_expansion_prompt:
            specific_prompt = concept_expansion_prompt
            print(f"[QueryExpander] Concept {concept_id}: using concept-specific expansion_prompt")
        else:
            specific_prompt = self.concept_specific_prompts.get(concept_id)
            if specific_prompt:
                print(f"[QueryExpander] Concept {concept_id}: using config-specific prompt")

        prompt = self._build_expansion_prompt(
            original_query, concept_name, concept_desc, specific_prompt
        )

        try:
            # Usar la biblioteca oficial de Ollama
            response = ollama.generate(
                model=self.model,
                prompt=prompt,
                options={
                    "temperature": 0.2,
                    "num_predict": 300,
                }
            )

            raw_output = response['response'].strip()
            print(f"[QueryExpander] Raw output for concept {concept_id}: {raw_output[:200]}...")

            # Extraer JSON array usando regex (más robusto)
            expanded_terms = self._extract_json_array(raw_output)

            if not expanded_terms:
                # Intentar con otro patrón más flexible
                expanded_terms = self._extract_json_array_flexible(raw_output)

            if not expanded_terms:
                raise ValueError("No valid JSON array found in response")

            concept["expanded_queries"] = expanded_terms[:self.max_terms]
            concept["original_query"] = original_query

            print(f"[QueryExpander] Concept {concept_id}: expanded '{original_query}' → +{len(expanded_terms)} terms")
            if expanded_terms:
                print(f"  → Terms: {expanded_terms[:3]}...")

        except Exception as e:
            print(f"[QueryExpander] Failed for concept {concept_id}: {e}")
            print(f"  Raw output was: {raw_output[:200]}...")
            concept["expanded_queries"] = []

        return concept

    def _extract_json_array(self, text: str) -> List[str]:
        """Extrae un JSON array del texto, normalizando comillas simples."""
        # Buscar patrón de array con comillas simples o dobles
        pattern = r'\[(.*?)\]'
        match = re.search(pattern, text, re.DOTALL)

        if match:
            array_content = match.group(1)

            # Normalizar: reemplazar comillas simples por dobles
            # Pero solo las que están alrededor de strings
            normalized = re.sub(r"'([^']*)'", r'"\1"', array_content)
            normalized = f"[{normalized}]"

            try:
                terms = json.loads(normalized)
                if isinstance(terms, list):
                    return [str(t) for t in terms]
            except json.JSONDecodeError:
                pass

        return []

    def _extract_json_array_flexible(self, text: str) -> List[str]:
        """Extrae JSON array de manera flexible, normalizando comillas."""
        # Limpiar markdown y código
        cleaned = re.sub(r'```json\s*|\s*```', '', text)
        cleaned = re.sub(r'^[^{[]*', '', cleaned)
        cleaned = re.sub(r'[^}\]]*$', '', cleaned)

        # Buscar array
        pattern = r'\[(.*?)\]'
        match = re.search(pattern, cleaned, re.DOTALL)

        if match:
            array_content = match.group(1)

            # Intentar extraer items con comillas simples o dobles
            # Primero probar con comillas dobles
            items = re.findall(r'"([^"]*)"', array_content)
            if not items:
                # Si no, probar con comillas simples
                items = re.findall(r"'([^']*)'", array_content)

            if items:
                return items

        return []

    def _build_expansion_prompt(self, query: str, name: str, desc: str, specific_prompt: str = None) -> str:
        """Construye el prompt para el LLM - versión estricta para JSON."""

        base_instruction = f"""You are a JSON generator. Respond ONLY with a valid JSON array. No explanations. No markdown. No code blocks.

Example: ["term1", "term2", "term3"]

Generate a JSON array of {self.max_terms} alternative phrases, synonyms, and related technical terms that would help retrieve relevant academic papers.

Requirements for each term:
- Short phrase (2-6 words)
- Technical and academic terminology
- Specific, not generic

Respond ONLY with the JSON array, nothing else."""

        if specific_prompt:
            return f"""{base_instruction}

SPECIFIC GUIDANCE: {specific_prompt}

Original concept: {query}"""

        return f"""{base_instruction}

Original concept: {query}
Concept name: {name}
Description: {desc}"""


def expand_all_concepts(aligned_concepts: Dict, config: Dict = None, config_file: str = None) -> Dict:
    """Expande todas las queries en aligned_concepts."""
    expander = QueryExpander(config, config_file)

    if not expander.enabled:
        print("[QueryExpander] Expansion disabled, skipping...")
        return aligned_concepts

    expanded_concepts = []
    for concept in aligned_concepts.get("aligned_concepts", []):
        expanded_concept = expander.expand_query(concept)
        expanded_concepts.append(expanded_concept)

    aligned_concepts["aligned_concepts"] = expanded_concepts
    return aligned_concepts