"""
translationService.py
=====================
Servicio de traducción de textos académicos de inglés a español
utilizando deep_translator (Google Translate) con caché en disco.
No usa LLM, es rápido y sin ruido de razonamiento.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from deep_translator import GoogleTranslator

from sources.common.common import logger, writeLog


class TranslationService:
    """
    Servicio de traducción con caché en disco.
    Traduce textos del inglés al español usando Google Translate.
    """

    def __init__(
        self,
        cache_file: Optional[Path] = None,
        source: str = "en",
        target: str = "es",
    ) -> None:
        self.source = source
        self.target = target
        self.translator = GoogleTranslator(source=source, target=target)
        self.cache: Dict[str, str] = {}
        self.cache_file = cache_file
        if cache_file and cache_file.exists():
            self.load_cache()

    def load_cache(self) -> None:
        """Carga la caché de traducciones desde el archivo en disco."""
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                self.cache = json.load(f)
            writeLog(
                "info",
                logger,
                f"[TranslationService] Loaded {len(self.cache)} cached translations",
            )
        except Exception as e:
            writeLog("warning", logger, f"[TranslationService] Could not load cache: {e}")
            self.cache = {}

    def save_cache(self) -> None:
        """Guarda la caché de traducciones en disco."""
        if self.cache_file:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
            writeLog(
                "info",
                logger,
                f"[TranslationService] Saved {len(self.cache)} translations",
            )

    def translate(self, text: str) -> str:
        """
        Traduce un texto del inglés al español.
        Si el texto ya está en caché, devuelve el valor almacenado.
        """
        if not text or len(text.strip()) < 10:
            return text

        clean = re.sub(r"\s+", " ", text).strip()
        key = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]

        if key in self.cache:
            return self.cache[key]

        try:
            translated = self.translator.translate(clean)
        except Exception as e:
            writeLog("warning", logger, f"[TranslationService] Translation failed: {e}")
            translated = text

        self.cache[key] = translated
        return translated

    def translate_batch(self, texts: List[str]) -> List[str]:
        """Traduce una lista de textos (con caché por cada uno)."""
        return [self.translate(t) for t in texts]