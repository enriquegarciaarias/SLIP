"""
newsClassifier.py
=================
Clasifica artículos de noticias según tipo (noticia, opinión, editorial, etc.),
relevancia para la investigación y agrupación temática.

Entradas:
  - raw_news.json (de newsCollector)
  - studyDescription.json (focus_terms, umbrales)

Salidas:
  - classified_news.json (artículos con campos añadidos)

Configuración en studyDescription.json:
    "news": {
        "classification_categories": ["news", "opinion", "editorial", "blog", "social", "press_release"],
        "relevance_threshold": 0.5,
        "use_llm_classification": true
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field

from sources.common.common import logger, writeLog
from sources.common.llm_client import create_resilient_ollama_client
from sources.common.utils import inicioModulo, read_json, write_json


@dataclass
class NewsClassifierConfig:
    categories: List[str] = field(default_factory=lambda: ["news", "opinion", "editorial", "blog", "social", "press_release"])
    relevance_threshold: float = 0.5
    use_llm_classification: bool = True

    @classmethod
    def from_project_config(cls, project: Dict) -> "NewsClassifierConfig":
        n = project.get("news", {})
        return cls(
            categories=n.get("classification_categories", cls.categories),
            relevance_threshold=n.get("relevance_threshold", 0.5),
            use_llm_classification=n.get("use_llm_classification", True)
        )

    @classmethod
    def from_file(cls, study_file: Path) -> "NewsClassifierConfig":
        if not study_file.exists():
            return cls()
        with open(study_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_project_config(data.get("project", {}))


class NewsClassifier:
    def __init__(self, config: NewsClassifierConfig, focus_terms: List[str]):
        self._cfg = config
        self._focus_terms = focus_terms
        self._llm = None if not config.use_llm_classification else create_resilient_ollama_client(temperature=0.0, max_tokens=200)

    def classify(self, articles: List[Dict]) -> List[Dict]:
        """Clasifica cada artículo y añade campos."""
        classified = []
        for art in articles:
            art_copy = art.copy()
            # Clasificación por LLM o heurística
            if self._cfg.use_llm_classification and self._llm:
                cat, rel, topic = self._classify_with_llm(art_copy)
            else:
                cat, rel, topic = self._classify_heuristic(art_copy)

            art_copy["category"] = cat
            art_copy["relevance_score"] = rel
            art_copy["topic_cluster"] = topic
            art_copy["focus_match"] = any(t.lower() in art_copy["text"].lower() for t in self._focus_terms)

            if rel >= self._cfg.relevance_threshold:
                classified.append(art_copy)

        writeLog("info", logger, f"[NewsClassifier] Classified {len(classified)} articles (threshold {self._cfg.relevance_threshold})")
        return classified

    def _classify_with_llm(self, article: Dict) -> tuple[str, float, str]:
        """Usa el LLM para clasificar categoría, relevancia y tema."""
        prompt = (
            f"Article: {article['title']}\n{article['text'][:1000]}\n\n"
            f"Classify this article into one of these categories: {', '.join(self._cfg.categories)}.\n"
            f"Also assign a relevance score (0-1) to the research focus: {', '.join(self._focus_terms) if self._focus_terms else 'general'}.\n"
            f"Provide a brief topic label (max 5 words).\n"
            f"Return JSON: {{'category': '...', 'relevance': 0.0, 'topic': '...'}}"
        )
        try:
            raw = self._llm.generate_json(
                prompt=prompt,
                system_prompt="You are a news classifier. Return only JSON.",
                temperature=0.0,
                max_tokens=150,
                context=f"classify_{hashlib.md5(article['doc_id'].encode()).hexdigest()[:8]}",
                expect_array=False
            )
            cat = raw.get("category", "news")
            rel = float(raw.get("relevance", 0.5))
            topic = raw.get("topic", "general")
            return cat, rel, topic
        except Exception as e:
            writeLog("warning", logger, f"[NewsClassifier] LLM error: {e}. Falling back heuristic.")
            return self._classify_heuristic(article)

    def _classify_heuristic(self, article: Dict) -> tuple[str, float, str]:
        """Clasificación básica por keywords."""
        text = (article["title"] + " " + article["text"]).lower()
        # Categoría simple
        if "opinion" in text or "perspective" in text:
            cat = "opinion"
        elif "editorial" in text:
            cat = "editorial"
        elif "blog" in text or "post" in text:
            cat = "blog"
        elif "press release" in text or "announce" in text:
            cat = "press_release"
        else:
            cat = "news"

        # Relevancia basada en focus terms
        rel = 0.0
        if self._focus_terms:
            matches = sum(1 for t in self._focus_terms if t.lower() in text)
            rel = min(1.0, matches / max(1, len(self._focus_terms) * 0.3))
        else:
            rel = 0.5

        # Tema: extraer algunas palabras clave
        words = [w for w in text.split() if len(w) > 4 and w not in {"https", "http", "www", "com"}]
        topic = " ".join(words[:5]) if words else "general"
        return cat, rel, topic[:30]


def processNewsClassifier() -> Optional[List[Dict]]:
    """Orquesta la clasificación y guarda classified_news.json."""
    input_dir, output_dir = inicioModulo("processNewsClassifier")

    raw_file = output_dir / "raw_news.json"
    if not raw_file.exists():
        writeLog("error", logger, f"[NewsClassifier] {raw_file} not found. Run newsCollector first.")
        return None

    articles = read_json(raw_file)
    study_data = read_json(input_dir / "studyDescription.json") if (input_dir / "studyDescription.json").exists() else {}
    config = NewsClassifierConfig.from_project_config(study_data.get("project", {}))
    focus_terms = study_data.get("project", {}).get("focus_terms", [])

    classifier = NewsClassifier(config, focus_terms)
    classified = classifier.classify(articles)

    output_file = output_dir / "classified_news.json"
    write_json(output_file, classified)
    writeLog("info", logger, f"[NewsClassifier] Saved {len(classified)} articles to {output_file}")
    return classified


if __name__ == "__main__":
    processNewsClassifier()