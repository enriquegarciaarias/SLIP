"""
translationService.py
=====================
Servicio de traducción de textos académicos de inglés a español.

Backends soportados:
  - "marian" (por defecto): modelo local Helsinki-NLP/opus-mt-en-es sobre
    transformers + sentencepiece. Rápido, determinista y sin límites de red.
  - "google": deep_translator (Google Translate). Se usa como respaldo si el
    backend local no está disponible o falla.

Incluye caché en disco y detección de textos ya en español para no
retraducirlos. Los fallos NO se cachean, de modo que puedan reintentarse.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from sources.common.common import logger, writeLog

# Límite práctico de caracteres por petición de Google Translate.
_MAX_CHARS_PER_REQUEST = 4500

# Marcadores léxicos inequívocos de español.
_ES_MARKERS = re.compile(
    r"\b(que|para|los|las|del|una|pero|también|sobre|entre|está|están|"
    r"más|como|con|por|sus|este|esta|estos|estas|desde|hasta|según|"
    r"mediante|dicha|dicho|así|ser|son|fue|han|siendo|cuando|donde)\b",
    re.IGNORECASE,
)
_ES_DIACRITICS = re.compile(r"[áéíóúñ¿¡ü]", re.IGNORECASE)


class _MarianTranslator:
    """Envoltura perezosa del modelo local MarianMT en->es."""

    def __init__(self, model_name: str, batch_size: int = 16, max_length: int = 512) -> None:
        import torch
        from transformers import MarianMTModel, MarianTokenizer

        self._torch = torch
        self._tokenizer = MarianTokenizer.from_pretrained(model_name)
        self._model = MarianMTModel.from_pretrained(model_name)
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model.to(self._device)
        self._model.eval()
        self._batch_size = batch_size
        self._max_length = max_length
        writeLog("info", logger, f"[TranslationService] MarianMT cargado en {self._device}")

    def translate_batch(self, texts: List[str]) -> List[str]:
        results: List[str] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            encoded = self._tokenizer(
                batch,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=self._max_length,
            ).to(self._device)
            with self._torch.no_grad():
                generated = self._model.generate(
                    **encoded, max_length=self._max_length
                )
            results.extend(
                self._tokenizer.batch_decode(generated, skip_special_tokens=True)
            )
        return results


class TranslationService:
    """
    Servicio de traducción con caché en disco.
    """

    # Intervalo mínimo entre peticiones (segundos) para no superar el límite.
    _MIN_INTERVAL = 0.30
    _MAX_RETRIES = 4

    def __init__(
        self,
        cache_file: Optional[Path] = None,
        source: str = "en",
        target: str = "es",
        backend: str = "marian",
        model_name: str = "Helsinki-NLP/opus-mt-en-es",
    ) -> None:
        self.source = source
        self.target = target
        self.backend = backend
        self.model_name = model_name
        self.cache: Dict[str, str] = {}
        self.cache_file = cache_file
        self._last_request_ts = 0.0
        self._google = None
        self._local: Optional[_MarianTranslator] = None
        self._local_failed = False
        if cache_file and cache_file.exists():
            self.load_cache()

    # ------------------------------------------------------------------
    # Caché en disco
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def translate(self, text: str) -> str:
        """
        Traduce un texto del inglés al español.
        Si ya está en español se devuelve tal cual; si está en caché se
        reutiliza. Si todos los backends fallan se devuelve el original y NO
        se cachea el fallo.
        """
        if not text or len(text.strip()) < 10:
            return text

        clean = re.sub(r"\s+", " ", text).strip()

        if self._looks_spanish(clean):
            return text

        key = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]
        if key in self.cache:
            return self.cache[key]

        translated = self._translate_with_backends(clean)
        if translated is None:
            writeLog(
                "warning",
                logger,
                "[TranslationService] Translation failed; keeping original "
                "text (not cached).",
            )
            return text

        self.cache[key] = translated
        return translated

    def translate_batch(self, texts: List[str]) -> List[str]:
        """Traduce una lista de textos (con caché por cada uno)."""
        return [self.translate(t) for t in texts]

    # ------------------------------------------------------------------
    # Backends
    # ------------------------------------------------------------------

    def _translate_with_backends(self, text: str) -> Optional[str]:
        if self.backend == "marian":
            result = self._translate_local(text)
            if result is not None:
                return result
            writeLog(
                "warning",
                logger,
                "[TranslationService] MarianMT no disponible; usando Google.",
            )
        return self._translate_google_chunked(text)

    def _translate_local(self, text: str) -> Optional[str]:
        if self._local_failed:
            return None
        if self._local is None:
            try:
                self._local = _MarianTranslator(self.model_name)
            except Exception as e:
                writeLog(
                    "warning",
                    logger,
                    f"[TranslationService] No se pudo cargar MarianMT: {e}",
                )
                self._local_failed = True
                return None
        try:
            return self._local.translate_batch([text])[0]
        except Exception as e:
            writeLog("warning", logger, f"[TranslationService] MarianMT falló: {e}")
            return None

    def _translate_google_chunked(self, text: str) -> Optional[str]:
        if len(text) <= _MAX_CHARS_PER_REQUEST:
            return self._google_request(text)

        parts = self._split(text, _MAX_CHARS_PER_REQUEST)
        translated_parts: List[str] = []
        for part in parts:
            result = self._google_request(part)
            if result is None:
                return None
            translated_parts.append(result)
        return " ".join(translated_parts)

    def _google_request(self, text: str) -> Optional[str]:
        if self._google is None:
            try:
                from deep_translator import GoogleTranslator

                self._google = GoogleTranslator(source=self.source, target=self.target)
            except Exception as e:
                writeLog("warning", logger, f"[TranslationService] Google no disponible: {e}")
                return None

        delay = 1.0
        for attempt in range(self._MAX_RETRIES):
            self._throttle()
            try:
                return self._google.translate(text)
            except Exception as e:
                wait = delay * (2 ** attempt)
                writeLog(
                    "warning",
                    logger,
                    f"[TranslationService] Google intento {attempt + 1}/"
                    f"{self._MAX_RETRIES} falló ({e}); reintento en {wait:.1f}s",
                )
                time.sleep(wait)
        return None

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_ts
        if elapsed < self._MIN_INTERVAL:
            time.sleep(self._MIN_INTERVAL - elapsed)
        self._last_request_ts = time.time()

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_spanish(text: str, min_markers: int = 3) -> bool:
        """Heurística para no retraducir textos que ya están en español."""
        if len(_ES_DIACRITICS.findall(text)) >= 3:
            return True
        markers = {m.group(0).lower() for m in _ES_MARKERS.finditer(text)}
        return len(markers) >= min_markers

    @staticmethod
    def _split(text: str, max_len: int) -> List[str]:
        """Trocea respetando límites de frase cuando es posible."""
        sentences = re.split(r"(?<=[.!?])\s+", text)
        chunks: List[str] = []
        current = ""
        for sentence in sentences:
            while len(sentence) > max_len:
                # Frase gigante: cortar por espacios
                cut = sentence.rfind(" ", 0, max_len)
                cut = cut if cut > 0 else max_len
                piece, sentence = sentence[:cut], sentence[cut:].lstrip()
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(piece)
            if not current:
                current = sentence
            elif len(current) + 1 + len(sentence) <= max_len:
                current += " " + sentence
            else:
                chunks.append(current)
                current = sentence
        if current:
            chunks.append(current)
        return chunks
