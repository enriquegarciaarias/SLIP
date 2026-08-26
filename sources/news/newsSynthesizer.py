"""
newsSynthesizer.py
==================
Sintetiza hallazgos a partir de noticias enriquecidas, alineándolas con
los conceptos (preguntas de investigación) del estudio.

Entradas:
  - enriched_news.json
  - conceptsQuery.json (preguntas de investigación)
  - studyDescription.json (focus_terms)

Salidas:
  - news_findings.json (hallazgos por concepto, con origen "news")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field

import numpy as np
from sentence_transformers import SentenceTransformer

from sources.common.common import logger, writeLog
from sources.common.llm_client import create_resilient_ollama_client
from sources.common.utils import inicioModulo, read_json, write_json


@dataclass
class NewsSynthesisConfig:
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    relevance_threshold: float = 0.35
    max_news_per_finding: int = 5
    max_findings_per_concept: int = 3
    use_llm: bool = True

    @classmethod
    def from_project_config(cls, project: Dict) -> "NewsSynthesisConfig":
        n = project.get("news", {})
        return cls(
            embedding_model=n.get("embedding_model", cls.embedding_model),
            relevance_threshold=n.get("relevance_threshold", 0.35),
            max_news_per_finding=n.get("max_news_per_finding", 5),
            max_findings_per_concept=n.get("max_findings_per_concept", 3),
            use_llm=n.get("synthesis_use_llm", True)
        )

    @classmethod
    def from_file(cls, study_file: Path) -> "NewsSynthesisConfig":
        if not study_file.exists():
            return cls()
        with open(study_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_project_config(data.get("project", {}))


class NewsSynthesizer:
    def __init__(self, config: NewsSynthesisConfig, focus_terms: List[str]):
        self._cfg = config
        self._focus_terms = focus_terms
        self._model = SentenceTransformer(config.embedding_model)
        self._llm = create_resilient_ollama_client(temperature=0.3, max_tokens=500) if config.use_llm else None

    def synthesize(self, concepts: List[Dict], news: List[Dict]) -> List[Dict]:
        """
        Para cada concepto, selecciona noticias relevantes y genera hallazgos.
        """
        # Preparar embeddings de noticias
        news_texts = [n["title"] + " " + n["text"][:1000] for n in news]
        news_embs = self._model.encode(news_texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False)

        results = []
        for concept in concepts:
            concept_query = concept.get("query", "")
            concept_emb = self._model.encode(concept_query, normalize_embeddings=True)

            # Calcular similitudes
            sims = np.dot(news_embs, concept_emb)
            # Seleccionar noticias relevantes
            relevant_indices = [i for i, s in enumerate(sims) if s >= self._cfg.relevance_threshold]
            relevant_indices = sorted(relevant_indices, key=lambda i: sims[i], reverse=True)[:self._cfg.max_findings_per_concept * self._cfg.max_news_per_finding]

            if not relevant_indices:
                continue

            # Agrupar noticias en clusters temáticos (usando similitud entre ellas)
            selected_news = [news[i] for i in relevant_indices]
            selected_sims = [float(sims[i]) for i in relevant_indices]

            # Generar hallazgos (puede ser uno o varios)
            findings = self._generate_findings(concept, selected_news, selected_sims)

            results.append({
                "concept_id": concept.get("id", "unknown"),
                "concept_query": concept_query,
                "news_findings": findings,
                "source_news": [{"doc_id": n["doc_id"], "title": n["title"], "relevance": s} for n, s in zip(selected_news, selected_sims)]
            })

        writeLog("info", logger, f"[NewsSynthesizer] Generated findings for {len(results)} concepts")
        return results

    def _generate_findings(self, concept: Dict, news_items: List[Dict], scores: List[float]) -> List[Dict]:
        """Genera uno o varios hallazgos narrativos a partir de las noticias."""
        if not self._cfg.use_llm or not self._llm:
            # Fallback: crear un hallazgo simple concatenando títulos
            titles = [n["title"] for n in news_items[:self._cfg.max_news_per_finding]]
            return [{
                "finding": f"Relevant news: {'; '.join(titles)}",
                "source_type": "news",
                "supporting_news": [n["doc_id"] for n in news_items[:self._cfg.max_news_per_finding]]
            }]

        # Intentar agrupar noticias en clusters (usando embeddings)
        # Con el fin de simplificar, se genera un único hallazgo con los más relevantes
        top_news = news_items[:self._cfg.max_news_per_finding]
        context = "\n\n".join([f"Title: {n['title']}\nContent: {n['text'][:600]}" for n in top_news])

        prompt = (
            f"Research question: {concept.get('query', '')}\n\n"
            f"Relevant news articles (with titles and snippets):\n{context}\n\n"
            f"Based on these news articles, synthesize a brief finding (in English) that summarizes "
            f"the key events, public perception, or practical implications related to the research question. "
            f"Keep it concise (2-4 sentences).\n\nFinding:"
        )

        try:
            raw = self._llm.generate_text(
                prompt=prompt,
                system_prompt="You are synthesizing news findings for research. Base your answer only on the provided news.",
                temperature=0.3,
                max_tokens=200,
                context=f"news_finding_{concept.get('id', '')}"
            )
            finding_text = raw.strip()
            if not finding_text:
                finding_text = "; ".join([n["title"] for n in top_news])
            return [{
                "finding": finding_text,
                "source_type": "news",
                "supporting_news": [n["doc_id"] for n in top_news],
                "avg_relevance": np.mean(scores[:len(top_news)]).item()
            }]
        except Exception as e:
            writeLog("warning", logger, f"[NewsSynthesizer] LLM error: {e}. Using fallback.")
            return [{
                "finding": "; ".join([n["title"] for n in top_news]),
                "source_type": "news",
                "supporting_news": [n["doc_id"] for n in top_news]
            }]


def processNewsSynthesizer() -> Optional[List[Dict]]:
    """Orquesta la síntesis y guarda news_findings.json."""
    input_dir, output_dir = inicioModulo("processNewsSynthesizer")

    # Cargar datos
    news_file = output_dir / "enriched_news.json"
    if not news_file.exists():
        writeLog("error", logger, f"[NewsSynthesizer] {news_file} not found. Run newsEnricher first.")
        return None

    concepts_file = input_dir / "conceptsQuery.json"
    if not concepts_file.exists():
        writeLog("error", logger, f"[NewsSynthesizer] {concepts_file} not found.")
        return None

    news = read_json(news_file)
    concepts_data = read_json(concepts_file)
    concepts = concepts_data.get("concepts", [])

    study_data = read_json(input_dir / "studyDescription.json") if (input_dir / "studyDescription.json").exists() else {}
    config = NewsSynthesisConfig.from_project_config(study_data.get("project", {}))
    focus_terms = study_data.get("project", {}).get("focus_terms", [])

    synthesizer = NewsSynthesizer(config, focus_terms)
    findings = synthesizer.synthesize(concepts, news)

    output_file = output_dir / "news_findings.json"
    write_json(output_file, findings)
    writeLog("info", logger, f"[NewsSynthesizer] Saved findings to {output_file}")
    return findings


if __name__ == "__main__":
    processNewsSynthesizer()