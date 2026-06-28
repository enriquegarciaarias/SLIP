"""
newsCollector.py
================
Recopila noticias, artículos de opinión y publicaciones de redes sociales
de fuentes configuradas (APIs, RSS, etc.) para su posterior análisis.

Entradas:
  - studyDescription.json (configuración de fuentes, keywords, límites)
  - focus_terms (para filtrar relevancia inicial)

Salidas:
  - raw_news.json (lista de artículos con metadatos)

Configuración en studyDescription.json:
    "news": {
        "sources": [
            {"type": "newsapi", "api_key": "xxx", "query": "AI OR machine learning", "language": "en", "page_size": 100},
            {"type": "rss", "url": "https://feeds.feedburner.com/example", "limit": 50},
            {"type": "twitter", "bearer_token": "xxx", "query": "AI ethics", "max_results": 50}
        ],
        "max_articles_total": 500,
        "focus_terms_boost": true
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
import hashlib

import requests
import feedparser

from sources.common.common import logger, writeLog
from sources.common.utils import inicioModulo, read_json, write_json


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

@dataclass
class NewsCollectorConfig:
    """Configuración de recolección de noticias."""
    max_articles_total: int = 500
    focus_terms_boost: bool = True
    sources: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_project_config(cls, project: Dict) -> "NewsCollectorConfig":
        n = project.get("news", {})
        return cls(
            max_articles_total=n.get("max_articles_total", 500),
            focus_terms_boost=n.get("focus_terms_boost", True),
            sources=n.get("sources", [])
        )

    @classmethod
    def from_file(cls, study_file: Path) -> "NewsCollectorConfig":
        if not study_file.exists():
            writeLog("warning", logger, "[NewsCollector] studyDescription.json not found. Using defaults.")
            return cls()
        with open(study_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_project_config(data.get("project", {}))


# ---------------------------------------------------------------------------
# Recolectores de fuentes
# ---------------------------------------------------------------------------

class NewsAPISource:
    def __init__(self, config: Dict):
        self.api_key = config.get("api_key")
        self.query = config.get("query", "technology")
        self.language = config.get("language", "en")
        self.page_size = min(config.get("page_size", 100), 100)
        self.base_url = "https://newsapi.org/v2/everything"

    def fetch(self, focus_terms: List[str] = None) -> List[Dict]:
        if not self.api_key:
            writeLog("warning", logger, "[NewsAPI] No API key provided. Skipping.")
            return []

        params = {
            "q": self.query,
            "language": self.language,
            "pageSize": self.page_size,
            "sortBy": "relevancy",
            "apiKey": self.api_key
        }
        try:
            resp = requests.get(self.base_url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            articles = data.get("articles", [])
            writeLog("info", logger, f"[NewsAPI] Fetched {len(articles)} articles.")
            return self._normalize(articles, "newsapi")
        except Exception as e:
            writeLog("error", logger, f"[NewsAPI] Error: {e}")
            return []

    def _normalize(self, articles: List[Dict], source_type: str) -> List[Dict]:
        normalized = []
        for a in articles:
            text = (a.get("title", "") + " " + a.get("description", "") + " " + a.get("content", "")).strip()
            if len(text) < 100:
                continue
            normalized.append({
                "source_type": source_type,
                "source_name": a.get("source", {}).get("name", "Unknown"),
                "title": a.get("title", ""),
                "text": text[:5000],
                "url": a.get("url", ""),
                "publication_date": a.get("publishedAt", ""),
                "author": a.get("author", ""),
                "raw_metadata": a,
                "language": self.language,
                "doc_id": f"newsapi_{hashlib.md5(text[:200].encode()).hexdigest()[:8]}"
            })
        return normalized


class RSSSource:
    def __init__(self, config: Dict):
        self.url = config.get("url")
        self.limit = config.get("limit", 50)

    def fetch(self, focus_terms: List[str] = None) -> List[Dict]:
        if not self.url:
            return []
        try:
            feed = feedparser.parse(self.url)
            entries = feed.entries[:self.limit]
            writeLog("info", logger, f"[RSS] Fetched {len(entries)} entries from {self.url}")
            return self._normalize(entries, "rss")
        except Exception as e:
            writeLog("error", logger, f"[RSS] Error: {e}")
            return []

    def _normalize(self, entries: List[Dict], source_type: str) -> List[Dict]:
        normalized = []
        for e in entries:
            text = (e.get("title", "") + " " + e.get("summary", "") + " " + e.get("content", [{}])[0].get("value", "")).strip()
            if len(text) < 100:
                continue
            normalized.append({
                "source_type": source_type,
                "source_name": e.get("source", {}).get("title", e.get("feed", {}).get("title", "Unknown RSS")),
                "title": e.get("title", ""),
                "text": text[:5000],
                "url": e.get("link", ""),
                "publication_date": e.get("published", ""),
                "author": e.get("author", ""),
                "raw_metadata": e,
                "language": "en",  # asumimos, se puede mejorar
                "doc_id": f"rss_{hashlib.md5(text[:200].encode()).hexdigest()[:8]}"
            })
        return normalized


# ---------------------------------------------------------------------------
# Recolector principal
# ---------------------------------------------------------------------------

class NewsCollector:
    def __init__(self, config: NewsCollectorConfig, focus_terms: List[str]):
        self._cfg = config
        self._focus_terms = focus_terms

    def collect(self) -> List[Dict]:
        """Recolecta artículos de todas las fuentes configuradas."""
        all_articles = []
        for source_conf in self._cfg.sources:
            source_type = source_conf.get("type")
            if source_type == "newsapi":
                source = NewsAPISource(source_conf)
                articles = source.fetch(self._focus_terms)
            elif source_type == "rss":
                source = RSSSource(source_conf)
                articles = source.fetch(self._focus_terms)
            else:
                writeLog("warning", logger, f"[NewsCollector] Unknown source type: {source_type}")
                continue
            all_articles.extend(articles)

        # Filtrar por focus terms si está activado
        if self._cfg.focus_terms_boost and self._focus_terms:
            filtered = []
            for art in all_articles:
                text_lower = art["text"].lower()
                score = sum(1 for t in self._focus_terms if t.lower() in text_lower)
                if score > 0:
                    art["focus_score"] = score
                    filtered.append(art)
                else:
                    # Opcional: mantener algunos aunque no tengan focus terms (para no perder contexto)
                    pass
            all_articles = filtered

        # Limitar total
        if len(all_articles) > self._cfg.max_articles_total:
            all_articles = all_articles[:self._cfg.max_articles_total]

        writeLog("info", logger, f"[NewsCollector] Total collected: {len(all_articles)} articles")
        return all_articles


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def processNewsCollector() -> Optional[List[Dict]]:
    """Orquesta la recolección y guarda raw_news.json."""
    input_dir, output_dir = inicioModulo("processNewsCollector")

    # Cargar configuración y focus_terms
    study_data = read_json(input_dir / "studyDescription.json") if (input_dir / "studyDescription.json").exists() else {}
    config = NewsCollectorConfig.from_project_config(study_data.get("project", {}))
    focus_terms = study_data.get("project", {}).get("focus_terms", [])

    collector = NewsCollector(config, focus_terms)
    articles = collector.collect()

    output_file = output_dir / "raw_news.json"
    write_json(output_file, articles)

    writeLog("info", logger, f"[NewsCollector] Saved {len(articles)} articles to {output_file}")
    return articles


if __name__ == "__main__":
    processNewsCollector()