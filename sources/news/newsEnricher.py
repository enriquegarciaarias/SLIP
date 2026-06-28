"""
newsEnricher.py
===============
Enriquece noticias clasificadas con información estructurada:
- Entidades (personas, organizaciones, lugares)
- Sentimiento (positivo, negativo, neutral)
- Stakeholders mencionados
- Implicaciones prácticas o riesgos
- Eventos clave

Entradas:
  - classified_news.json
  - studyDescription.json (opcional)

Salidas:
  - enriched_news.json (con campos estructurados)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Dict, Optional, Any, Set
from dataclasses import dataclass, field

from sources.common.common import logger, writeLog
from sources.common.llm_client import create_resilient_ollama_client
from sources.common.utils import inicioModulo, read_json, write_json


@dataclass
class NewsEnricherConfig:
    use_llm: bool = True
    extract_entities: bool = True
    sentiment_analysis: bool = True

    @classmethod
    def from_project_config(cls, project: Dict) -> "NewsEnricherConfig":
        n = project.get("news", {})
        return cls(
            use_llm=n.get("enrich_use_llm", True),
            extract_entities=n.get("extract_entities", True),
            sentiment_analysis=n.get("sentiment_analysis", True)
        )

    @classmethod
    def from_file(cls, study_file: Path) -> "NewsEnricherConfig":
        if not study_file.exists():
            return cls()
        with open(study_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_project_config(data.get("project", {}))


class NewsEnricher:
    def __init__(self, config: NewsEnricherConfig, focus_terms: List[str]):
        self._cfg = config
        self._focus_terms = focus_terms
        self._llm = create_resilient_ollama_client(temperature=0.0, max_tokens=300) if config.use_llm else None

    def enrich(self, articles: List[Dict]) -> List[Dict]:
        """Añade campos estructurados a cada artículo."""
        enriched = []
        for art in articles:
            copy = art.copy()
            if self._cfg.use_llm and self._llm:
                structured = self._enrich_with_llm(copy)
            else:
                structured = self._enrich_heuristic(copy)

            copy["structured_evidence"] = structured
            enriched.append(copy)

        writeLog("info", logger, f"[NewsEnricher] Enriched {len(enriched)} articles")
        return enriched

    def _enrich_with_llm(self, article: Dict) -> Dict:
        """Usa el LLM para extraer entidades, sentimiento, stakeholders, implicaciones."""
        prompt = (
            f"Article: {article['title']}\n{article['text'][:1500]}\n\n"
            f"Extract the following structured information:\n"
            f"- Entities: people, organizations, places (list)\n"
            f"- Sentiment: overall sentiment (positive, negative, neutral)\n"
            f"- Stakeholders: groups or individuals mentioned as having interest or influence\n"
            f"- Implications: practical consequences, risks, or opportunities mentioned\n"
            f"- Key events: any specific event described\n"
            f"Return JSON with keys: entities, sentiment, stakeholders, implications, key_events."
        )
        try:
            raw = self._llm.generate_json(
                prompt=prompt,
                system_prompt="You are a structured information extractor. Return only JSON.",
                temperature=0.0,
                max_tokens=300,
                context=f"enrich_{article['doc_id']}",
                expect_array=False
            )
            return {
                "entities": raw.get("entities", []),
                "sentiment": raw.get("sentiment", "neutral"),
                "stakeholders": raw.get("stakeholders", []),
                "implications": raw.get("implications", ""),
                "key_events": raw.get("key_events", ""),
                "extraction_source": "llm"
            }
        except Exception as e:
            writeLog("warning", logger, f"[NewsEnricher] LLM error: {e}. Falling back heuristic.")
            return self._enrich_heuristic(article)

    def _enrich_heuristic(self, article: Dict) -> Dict:
        """Extracción básica con regex y palabras clave."""
        text = article["text"].lower()

        # Entidades básicas: buscar patrones de mayúsculas
        entities = list(set(re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', article["text"])))[:5]

        # Sentimiento simple con palabras clave
        positive = ["good", "great", "excellent", "positive", "success", "benefit", "improve"]
        negative = ["bad", "poor", "negative", "risk", "danger", "fail", "loss", "critic"]
        pos_count = sum(1 for w in positive if w in text)
        neg_count = sum(1 for w in negative if w in text)
        if pos_count > neg_count:
            sentiment = "positive"
        elif neg_count > pos_count:
            sentiment = "negative"
        else:
            sentiment = "neutral"

        stakeholders = []
        for term in ["government", "company", "industry", "academia", "consumer", "regulator", "NGO"]:
            if term in text:
                stakeholders.append(term)

        return {
            "entities": entities,
            "sentiment": sentiment,
            "stakeholders": stakeholders,
            "implications": "",
            "key_events": "",
            "extraction_source": "heuristic"
        }


def processNewsEnricher() -> Optional[List[Dict]]:
    """Orquesta el enriquecimiento y guarda enriched_news.json."""
    input_dir, output_dir = inicioModulo("processNewsEnricher")

    classified_file = output_dir / "classified_news.json"
    if not classified_file.exists():
        writeLog("error", logger, f"[NewsEnricher] {classified_file} not found. Run newsClassifier first.")
        return None

    articles = read_json(classified_file)
    study_data = read_json(input_dir / "studyDescription.json") if (input_dir / "studyDescription.json").exists() else {}
    config = NewsEnricherConfig.from_project_config(study_data.get("project", {}))
    focus_terms = study_data.get("project", {}).get("focus_terms", [])

    enricher = NewsEnricher(config, focus_terms)
    enriched = enricher.enrich(articles)

    output_file = output_dir / "enriched_news.json"
    write_json(output_file, enriched)
    writeLog("info", logger, f"[NewsEnricher] Saved {len(enriched)} articles to {output_file}")
    return enriched


if __name__ == "__main__":
    processNewsEnricher()