"""
enrich_evidences.py
====================
Enriquece las evidencias clusterizadas con información estructurada
extraída de los papers, separando la captura de información del proceso
de síntesis (synthesize_findings.py).

Decisiones de diseño del proyecto:
  1. Sin traducciones aquí — las traducciones ocurren en el momento
     de presentación (construcción del .md en trainingMaterials.py).
     Los campos extraídos se almacenan en el idioma original del paper.
  2. Sin código LLM directo — toda inferencia usa el servicio federado
     llm_client.py (create_ollama_client / create_resilient_ollama_client).
  3. El LLM es fallback opcional para brecha/evolución cuando la cobertura
     regex es insuficiente (configurable por umbral).

Extrae seis dimensiones por paper:
  ideas        → contexto / problema abordado (Introduction/Background)
  metodos      → cómo lo resuelve (Methods/Methodology)
  resultados   → qué obtuvo (Results/Findings)
  aplicaciones → qué implica y concluye (Discussion/Conclusion)
  brecha       → gap de investigación identificado por el paper
  evolucion    → trabajos futuros propuestos por el paper

NUEVOS CAMPOS ESTRUCTURADOS (para estandarización de hallazgos):
  senales      → lista de señales fisiológicas/comportamentales usadas
  modelos      → lista de modelos de ML/DL/NLP empleados
  metricas     → diccionario con métricas de rendimiento (accuracy, F1, etc.)
  limitaciones → texto con limitaciones del estudio
  aplicacion   → contexto de aplicación (conducción autónoma, salud, etc.)

Configuración en studyDescription.json:
    "enrichment": {
        "max_section_length": 1500,
        "max_extracted_sentences": 3,
        "llm_fallback_threshold": 0.40,  // cobertura mínima regex antes de usar LLM
        "llm_fallback_enabled": true,    // false = nunca usar LLM aquí
        "section_map": {
            "ideas":        ["introduction", "background", "abstract"],
            "metodos":      ["methods", "methodology", "experimental", "procedure"],
            "resultados":   ["results", "findings", "evaluation"],
            "aplicaciones": ["discussion", "conclusion", "application"]
        },
        "gap_search_in":    ["ideas"],
        "future_search_in": ["aplicaciones", "resultados"],
        "signals_list": ["ECG", "EEG", "EDA", "GSR", "EMG", "EOG", "fNIRS", "HRV", "PPG", "pupillometry", "eye tracking", "respiration"],
        "models_list": ["SVM", "KNN", "Random Forest", "XGBoost", "CNN", "LSTM", "BERT", "Transformer", "ViT", "TCN", "GNN", "DBN", "GAN"]
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

from sources.common.common import logger, writeLog
from sources.common.utils import inicioModulo, read_json, write_json


# ---------------------------------------------------------------------------
# Patrones regex para brecha e impacto futuro (bilingüe ES/EN)
# ---------------------------------------------------------------------------

_GAP_PATTERNS: List[re.Pattern] = [
    re.compile(
        r'(?:brecha|gap|laguna)\s+(?:de investigaci[oó]n|en la literatura|significati[vw])',
        re.IGNORECASE),
    re.compile(
        r'(?:sin embargo|however)\s*,?[^.]{0,30}(?:brecha|gap|limitaci[oó]n|carencia|falta)',
        re.IGNORECASE),
    re.compile(
        r'(?:no existe[n]?|lack of|ausencia de|escasez de|pocos estudios|few studies|'
        r'limited research|escasa atenci[oó]n)[^.]{10,200}[.]',
        re.IGNORECASE | re.DOTALL),
    re.compile(
        r'(?:la mayor[ií]a de(?: los)? estudios|most(?: existing)? studies|'
        r'previous(?: work| studies)?)[^.]{10,200}'
        r'(?:limita|restrict|lab|controlled|artificial)[^.]*[.]',
        re.IGNORECASE | re.DOTALL),
    re.compile(
        r'(?:se necesita|se requiere|there is a need|it is necessary|'
        r'remains unclear|a[ú]n no se ha)[^.]{10,200}[.]',
        re.IGNORECASE | re.DOTALL),
]

_FUTURE_PATTERNS: List[re.Pattern] = [
    re.compile(
        r'(?:como trabajo futuro|as future work|en trabajos futuros|'
        r'future work (?:will|should|could|may))[^.]{10,300}[.]',
        re.IGNORECASE | re.DOTALL),
    re.compile(
        r'(?:trabajo futuro|future work|investigaci[oó]n futura|'
        r'future (?:research|studies|directions|work))[^.]{10,250}[.]',
        re.IGNORECASE | re.DOTALL),
    re.compile(
        r'(?:se planea|se propone|planemos|we plan|we intend|we will|'
        r'will be explored|will be investigated)[^.]{10,200}[.]',
        re.IGNORECASE | re.DOTALL),
    re.compile(
        r'(?:quedan pendientes|remain to be|still (?:need|needs|require)|'
        r'l[ií]neas futuras|future directions)[^.]{10,200}[.]',
        re.IGNORECASE | re.DOTALL),
    re.compile(
        r'(?:en el futuro|in the future|subsequently|'
        r'en estudios posteriores|in subsequent studies)[^.]{10,200}[.]',
        re.IGNORECASE | re.DOTALL),
]


# ---------------------------------------------------------------------------
# NUEVOS PATRONES para señales, modelos, métricas, limitaciones y aplicación
# ---------------------------------------------------------------------------

# Lista de señales fisiológicas/comportamentales típicas
_DEFAULT_SIGNALS = {
    "ECG", "electrocardiogram", "electrocardiograma",
    "EEG", "electroencephalogram", "electroencefalograma",
    "EDA", "electrodermal", "galvanic skin", "respuesta galvánica",
    "GSR", "galvanic skin response",
    "EMG", "electromyogram", "electromiograma",
    "EOG", "electrooculogram",
    "fNIRS", "functional near-infrared spectroscopy",
    "HRV", "heart rate variability", "variabilidad de la frecuencia cardíaca",
    "PPG", "photoplethysmogram",
    "pupillometry", "pupil diameter", "dilatación de la pupila",
    "eye tracking", "seguimiento ocular", "gaze", "mirada",
    "respiration", "respiración", "breathing",
    "skin temperature", "temperatura de la piel",
    "accelerometer", "acelerómetro", "movement", "movimiento"
}
_DEFAULT_SIGNALS_SET = {s.lower() for s in _DEFAULT_SIGNALS}

# Lista de modelos de ML/DL/NLP típicos
_DEFAULT_MODELS = {
    "SVM", "support vector machine",
    "KNN", "k-nearest neighbors",
    "Random Forest", "random forest",
    "XGBoost",
    "CNN", "convolutional neural network",
    "LSTM", "long short-term memory",
    "BERT",
    "Transformer", "transformers",
    "ViT", "vision transformer",
    "TCN", "temporal convolutional network",
    "GNN", "graph neural network",
    "DBN", "deep belief network",
    "GAN", "generative adversarial network",
    "MLP", "multilayer perceptron",
    "RNN", "recurrent neural network",
    "DNN", "deep neural network",
}
_DEFAULT_MODELS_SET = {m.lower() for m in _DEFAULT_MODELS}

# Patrón para métricas: "accuracy of X%", "precision X%", "F1-score X", etc.
_METRIC_PATTERN = re.compile(
    r'(?:accuracy|precisi[oó]n|recall|f1[ -]?score|exactitud|sensibilidad|specificity|'
    r'especificidad|roc|auc)[^0-9]*([0-9.]+)\s*%?',
    re.IGNORECASE
)

# Frases para limitaciones
_LIMITATION_PHRASES = re.compile(
    r'(?:limitation|limitaciones|limitation|challenge|desaf[ií]o|drawback|inconvenient|'
    r'shortcoming|deficiency|carencia|weakness|debilidad)',
    re.IGNORECASE
)

# Frases para aplicación
_APPLICATION_PHRASES = re.compile(
    r'(?:application|aplicaci[oó]n|use case|caso de uso|context|contexto|'
    r'setting|entorno|scenario|escenario)',
    re.IGNORECASE
)

def _extract_by_pattern(
    text: str,
    patterns: List[re.Pattern],
    max_sentences: int,
) -> str:
    """
    Extrae hasta max_sentences fragmentos que coincidan con algún patrón.
    Deduplicación por rango de posición para evitar que dos patrones
    distintos capturen el mismo fragmento con distinto span.
    """
    all_matches: List[Tuple[int, int, str]] = []

    for pattern in patterns:
        for m in pattern.finditer(text):
            fragment = m.group(0).strip()
            if len(fragment) > 30:
                all_matches.append((m.start(), m.end(), fragment))

    if not all_matches:
        return ""

    all_matches.sort(key=lambda x: x[0])
    selected: List[str] = []
    last_end = -1

    for start, end, fragment in all_matches:
        if start >= last_end:
            selected.append(fragment)
            last_end = end
            if len(selected) >= max_sentences:
                break

    return " ".join(selected)

def _extract_signals(text: str, signal_set: Set[str] = None) -> List[str]:
    """Extrae señales fisiológicas/comportamentales mencionadas en el texto."""
    if signal_set is None:
        signal_set = _DEFAULT_SIGNALS_SET
    found = set()
    # Buscar tokens que coincidan con las señales (case-insensitive)
    words = re.findall(r'[a-zA-Z\-]+', text.lower())
    for w in words:
        if w in signal_set or any(s in w for s in signal_set):
            # Mapear a nombre canónico (ej. "ecg" -> "ECG")
            for canonical in _DEFAULT_SIGNALS:
                if canonical.lower() == w or canonical.lower() in w:
                    found.add(canonical)
                    break
            else:
                found.add(w)
    # También buscar frases como "eye tracking"
    for phrase in ["eye tracking", "seguimiento ocular", "pupil dilation", "dilatación pupilar"]:
        if phrase in text.lower():
            found.add(phrase)
    return sorted(found)


def _extract_models(text: str, model_set: Set[str] = None) -> List[str]:
    """Extrae nombres de modelos de ML/DL/NLP mencionados en el texto."""
    if model_set is None:
        model_set = _DEFAULT_MODELS_SET
    found = set()
    words = re.findall(r'[a-zA-Z\-]+', text.lower())
    for w in words:
        if w in model_set or any(m in w for m in model_set):
            for canonical in _DEFAULT_MODELS:
                if canonical.lower() == w or canonical.lower() in w:
                    found.add(canonical)
                    break
            else:
                found.add(w)
    # Buscar siglas como "CNN", "LSTM" (ya capturadas por el bucle)
    return sorted(found)


def _extract_metrics(text: str) -> Dict[str, str]:
    """Extrae métricas de rendimiento (accuracy, precision, F1, etc.) como dict."""
    metrics = {}
    # Buscar todas las coincidencias de métricas
    for match in _METRIC_PATTERN.finditer(text):
        key = match.group(0).strip().split()[0]  # primera palabra como clave
        value = match.group(1) + "%" if match.group(1) else ""
        if key.lower() not in metrics:
            metrics[key.lower()] = value
    return metrics


def _extract_limitations(text: str, max_sentences: int = 2) -> str:
    """Extrae frases sobre limitaciones del estudio."""
    # Buscar oraciones que contengan palabras clave de limitación
    sentences = re.split(r'[.!?]+', text)
    limitations = []
    for sent in sentences:
        if _LIMITATION_PHRASES.search(sent):
            limitations.append(sent.strip())
            if len(limitations) >= max_sentences:
                break
    return ". ".join(limitations)


def _extract_application(text: str, max_sentences: int = 2) -> str:
    """Extrae frases sobre el contexto de aplicación."""
    sentences = re.split(r'[.!?]+', text)
    applications = []
    for sent in sentences:
        if _APPLICATION_PHRASES.search(sent):
            applications.append(sent.strip())
            if len(applications) >= max_sentences:
                break
    return ". ".join(applications)


# ---------------------------------------------------------------------------
# Configuración tipada (ampliada)
# ---------------------------------------------------------------------------

_DEFAULT_SECTION_MAP: Dict[str, List[str]] = {
    "ideas":        ["introduction", "background", "abstract"],
    "metodos":      ["methods", "methodology", "experimental", "procedure"],
    "resultados":   ["results", "findings", "evaluation"],
    "aplicaciones": ["discussion", "conclusion", "application"],
}


@dataclass
class EnrichmentConfig:
    """
    Parámetros de enriquecimiento. Todos configurables desde studyDescription.json.
    """
    max_section_length:      int   = 1500
    max_extracted_sentences: int   = 3
    llm_fallback_threshold:  float = 0.40  # cobertura mínima regex antes de activar LLM
    llm_fallback_enabled:    bool  = True  # False = nunca usar LLM en este módulo

    section_map: Dict[str, List[str]] = field(
        default_factory=lambda: dict(_DEFAULT_SECTION_MAP)
    )
    gap_search_in:    List[str] = field(default_factory=lambda: ["ideas"])
    future_search_in: List[str] = field(default_factory=lambda: ["aplicaciones", "resultados"])

    # NUEVOS: listas de términos para extracción estructurada
    signals_list: List[str] = field(default_factory=lambda: sorted(_DEFAULT_SIGNALS))
    models_list:  List[str] = field(default_factory=lambda: sorted(_DEFAULT_MODELS))

    @classmethod
    def from_project_config(cls, project: Dict) -> "EnrichmentConfig":
        e = project.get("enrichment", {})
        cfg = cls()
        if "max_section_length"      in e: cfg.max_section_length      = int(e["max_section_length"])
        if "max_extracted_sentences" in e: cfg.max_extracted_sentences = int(e["max_extracted_sentences"])
        if "llm_fallback_threshold"  in e: cfg.llm_fallback_threshold  = float(e["llm_fallback_threshold"])
        if "llm_fallback_enabled"    in e: cfg.llm_fallback_enabled    = bool(e["llm_fallback_enabled"])
        if "gap_search_in"           in e: cfg.gap_search_in           = list(e["gap_search_in"])
        if "future_search_in"        in e: cfg.future_search_in        = list(e["future_search_in"])
        if "signals_list"            in e: cfg.signals_list            = list(e["signals_list"])
        if "models_list"             in e: cfg.models_list             = list(e["models_list"])
        if "section_map" in e:
            merged = dict(_DEFAULT_SECTION_MAP)
            merged.update(e["section_map"])
            cfg.section_map = merged
        return cfg

    @classmethod
    def from_file(cls, study_file: Path) -> "EnrichmentConfig":
        if not study_file.exists():
            writeLog("warning", logger,
                     "[EnrichmentConfig] studyDescription.json not found. Using defaults.")
            return cls()
        with open(study_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_project_config(data.get("project", {}))

    def log_active_config(self) -> None:
        writeLog("info", logger, "[EnrichmentConfig] Configuración activa:")
        writeLog("info", logger, f"  max_section_length:      {self.max_section_length}")
        writeLog("info", logger, f"  max_extracted_sentences: {self.max_extracted_sentences}")
        writeLog("info", logger,
                 f"  llm_fallback:            "
                 f"{'activado (umbral=' + str(self.llm_fallback_threshold) + ')' if self.llm_fallback_enabled else 'desactivado'}")
        writeLog("info", logger,  "  section_map:")
        for key, candidates in self.section_map.items():
            writeLog("info", logger, f"    {key}: {candidates}")
        writeLog("info", logger, f"  gap_search_in:    {self.gap_search_in}")
        writeLog("info", logger, f"  future_search_in: {self.future_search_in}")
        writeLog("info", logger, f"  signals_list:     {len(self.signals_list)} términos")
        writeLog("info", logger, f"  models_list:      {len(self.models_list)} términos")


# ---------------------------------------------------------------------------
# Resultado de extracción (inmutable) AMPLIADO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExtractionResult:
    """
    Resultado de extraer las dimensiones de un paper.
    Texto en idioma original (sin traducir) — las traducciones
    ocurren en trainingMaterials.py en el momento de presentación.
    """
    paper_id: str
    ideas:        str = ""
    metodos:      str = ""
    resultados:   str = ""
    aplicaciones: str = ""
    brecha:       str = ""   # gap de investigación identificado por el paper
    evolucion:    str = ""   # trabajos futuros propuestos por el paper

    # NUEVOS CAMPOS ESTRUCTURADOS
    senales:      List[str] = field(default_factory=list)
    modelos:      List[str] = field(default_factory=list)
    metricas:     Dict[str, str] = field(default_factory=dict)
    limitaciones: str = ""
    aplicacion:   str = ""

    @property
    def is_empty(self) -> bool:
        return not any([self.ideas, self.metodos, self.resultados,
                        self.aplicaciones, self.brecha, self.evolucion,
                        self.senales, self.modelos, self.metricas,
                        self.limitaciones, self.aplicacion])

    @property
    def section_coverage(self) -> Dict[str, bool]:
        return {
            "ideas":        bool(self.ideas),
            "metodos":      bool(self.metodos),
            "resultados":   bool(self.resultados),
            "aplicaciones": bool(self.aplicaciones),
            "brecha":       bool(self.brecha),
            "evolucion":    bool(self.evolucion),
            "senales":      bool(self.senales),
            "modelos":      bool(self.modelos),
            "metricas":     bool(self.metricas),
            "limitaciones": bool(self.limitaciones),
            "aplicacion":   bool(self.aplicacion),
        }

    @property
    def n_populated(self) -> int:
        return sum(self.section_coverage.values())

    def needs_llm_fallback(self, config: "EnrichmentConfig") -> bool:
        """
        True si alguno de los campos considerados 'estructurales' está vacío.
        Por defecto: brecha, evolucion, senales, modelos, limitaciones, aplicacion.
        """
        return not (self.brecha and self.evolucion and self.senales and self.modelos
                    and self.limitaciones and self.aplicacion)

    def to_dict(self) -> Dict:
        """Incluye todos los campos con contenido (para structured_evidence)."""
        result = {}
        for field_name in ("ideas", "metodos", "resultados", "aplicaciones",
                           "brecha", "evolucion", "limitaciones", "aplicacion"):
            val = getattr(self, field_name)
            if val:
                result[field_name] = val
        for field_name in ("senales", "modelos"):
            val = getattr(self, field_name)
            if val:
                result[field_name] = val
        if self.metricas:
            result["metricas"] = self.metricas
        return result


# ---------------------------------------------------------------------------
# Extractor stateless (sin caché, sin LLM, sin estado) AMPLIADO
# ---------------------------------------------------------------------------

class PaperSectionExtractor:
    """
    Extrae las dimensiones de un paper. Stateless.

    Tecnología:
      ideas/metodos/resultados/aplicaciones → dict lookup O(1) sobre clean_sections
      brecha/evolucion → regex bilingüe sobre texto de secciones candidatas
      senales/modelos/metricas/limitaciones/aplicacion → regex específicos sobre texto combinado
    """

    def __init__(self, config: EnrichmentConfig) -> None:
        self._cfg = config
        # Preparar conjuntos para búsqueda rápida
        self._signal_set = {s.lower() for s in config.signals_list}
        self._model_set = {m.lower() for m in config.models_list}

    def extract(self, paper_id: str, paper: Optional[Dict]) -> ExtractionResult:
        if not paper:
            return ExtractionResult(paper_id=paper_id)

        raw_sections = paper.get("clean_sections", {})
        section_texts: Dict[str, str] = {}

        # Extraer secciones fijas
        for key, candidates in self._cfg.section_map.items():
            text = next((raw_sections[c] for c in candidates if raw_sections.get(c)), "")
            if not text and key == "ideas":
                text = paper.get("abstract", "") or paper.get("clean_text", "")[:2000]
            section_texts[key] = text[: self._cfg.max_section_length] if text else ""

        # Extraer brecha y evolución (sobre secciones específicas)
        brecha    = self._extract_gap(section_texts)
        evolucion = self._extract_future_work(section_texts)

        # Extraer señales, modelos, métricas, limitaciones, aplicación desde TODO el texto
        combined_text = "\n".join(section_texts.values()) + "\n" + paper.get("clean_text", "")[:3000]
        senales = self._extract_signals(combined_text)
        modelos = self._extract_models(combined_text)
        metricas = self._extract_metrics(combined_text)
        limitaciones = self._extract_limitations(combined_text)
        aplicacion = self._extract_application(combined_text)

        return ExtractionResult(
            paper_id=     paper_id,
            ideas=        section_texts.get("ideas", ""),
            metodos=      section_texts.get("metodos", ""),
            resultados=   section_texts.get("resultados", ""),
            aplicaciones= section_texts.get("aplicaciones", ""),
            brecha=       brecha,
            evolucion=    evolucion,
            senales=      senales,
            modelos=      modelos,
            metricas=     metricas,
            limitaciones= limitaciones,
            aplicacion=   aplicacion,
        )

    # Métodos de extracción específicos (usando las funciones auxiliares)

    def _extract_gap(self, section_texts: Dict[str, str]) -> str:
        for key in self._cfg.gap_search_in:
            text = section_texts.get(key, "")
            if text:
                result = _extract_by_pattern(text, _GAP_PATTERNS,
                                             self._cfg.max_extracted_sentences)
                if result:
                    return result
        return ""

    def _extract_future_work(self, section_texts: Dict[str, str]) -> str:
        for key in self._cfg.future_search_in:
            text = section_texts.get(key, "")
            if text:
                result = _extract_by_pattern(text, _FUTURE_PATTERNS,
                                             self._cfg.max_extracted_sentences)
                if result:
                    return result
        return ""

    def _extract_signals(self, text: str) -> List[str]:
        return _extract_signals(text, self._signal_set)

    def _extract_models(self, text: str) -> List[str]:
        return _extract_models(text, self._model_set)

    def _extract_metrics(self, text: str) -> Dict[str, str]:
        return _extract_metrics(text)

    def _extract_limitations(self, text: str) -> str:
        return _extract_limitations(text, self._cfg.max_extracted_sentences)

    def _extract_application(self, text: str) -> str:
        return _extract_application(text, self._cfg.max_extracted_sentences)


# ---------------------------------------------------------------------------
# Caché de extracciones
# ---------------------------------------------------------------------------

class ExtractionCache:
    def __init__(self, extractor: PaperSectionExtractor) -> None:
        self._extractor = extractor
        self._cache:    Dict[str, ExtractionResult] = {}
        self.hits   = 0
        self.misses = 0

    def get(self, paper_id: str, papers_text: Dict) -> ExtractionResult:
        if paper_id in self._cache:
            self.hits += 1
            return self._cache[paper_id]
        self.misses += 1
        result = self._extractor.extract(paper_id, papers_text.get(paper_id))
        self._cache[paper_id] = result
        return result

    def update(self, paper_id: str, result: ExtractionResult) -> None:
        self._cache[paper_id] = result

    def all_results(self) -> List[ExtractionResult]:
        return list(self._cache.values())

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def log_stats(self) -> None:
        writeLog("info", logger,
                 f"[ExtractionCache] {self.misses} papers extraídos, "
                 f"{self.hits} hits de caché "
                 f"({self.hit_rate*100:.0f}% ahorro)")


# ---------------------------------------------------------------------------
# LLM Fallback (segunda pasada) AMPLIADO para nuevos campos
# ---------------------------------------------------------------------------

class LLMFallbackEnricher:
    """
    Usa el LLM federado para extraer TODOS los campos estructurados
    en los papers donde la cobertura regex sea insuficiente.
    """

    _SYSTEM_PROMPT = (
        "You are a structured information extractor for academic papers. "
        "Extract ONLY what is explicitly stated in the provided text. "
        "Do not infer, summarize, or add external knowledge. "
        "Return a JSON object with these keys:\n"
        "  'brecha'      : research gap identified (1-3 sentences, original language)\n"
        "  'evolucion'   : future work proposed (1-3 sentences, original language)\n"
        "  'senales'     : list of physiological/behavioral signals used (e.g., ['ECG','EEG'])\n"
        "  'modelos'     : list of machine learning models used (e.g., ['SVM','LSTM'])\n"
        "  'metricas'    : dict with performance metrics (e.g., {'accuracy':'89.9%'})\n"
        "  'limitaciones': limitations of the study (1-2 sentences, original language)\n"
        "  'aplicacion'  : application context (1-2 sentences, original language)\n"
        "Use empty string or empty list if information is not present."
    )

    def __init__(self, config: EnrichmentConfig) -> None:
        self._cfg = config
        self._llm = None

    def _get_llm(self):
        if self._llm is None:
            from sources.common.llm_client import create_resilient_ollama_client
            self._llm = create_resilient_ollama_client(
                temperature=0.0,
                max_tokens=600,
            )
            writeLog("info", logger,
                     "[LLMFallbackEnricher] LLM client initialized (lazy)")
        return self._llm

    def enrich_missing(
        self,
        cache: ExtractionCache,
        papers_text: Dict,
        config: EnrichmentConfig,
    ) -> int:
        """
        Recorre el caché y lanza el LLM para papers que necesitan mejora.
        Actualiza el caché in-place.
        Devuelve el número de papers procesados.
        """
        candidates = [r for r in cache.all_results() if r.needs_llm_fallback(config)]

        if not candidates:
            writeLog("info", logger,
                     "[LLMFallbackEnricher] No papers need LLM fallback")
            return 0

        writeLog("info", logger,
                 f"[LLMFallbackEnricher] Running LLM fallback for "
                 f"{len(candidates)} papers with incomplete extraction")

        llm = self._get_llm()
        processed = 0

        for result in candidates:
            paper = papers_text.get(result.paper_id)
            if not paper:
                continue

            context_text = self._build_context(result)
            if not context_text.strip():
                continue

            prompt = (
                f"Extract the following information from this academic text. "
                f"Return valid JSON with keys: brecha, evolucion, senales, modelos, metricas, limitaciones, aplicacion.\n\n"
                f"Text:\n{context_text}\n\n"
                f'Return JSON: {{"brecha": "...", "evolucion": "...", '
                f'"senales": [...], "modelos": [...], '
                f'"metricas": {{...}}, "limitaciones": "...", "aplicacion": "..."}}'
            )

            raw = llm.generate_json(
                prompt=prompt,
                system_prompt=self._SYSTEM_PROMPT,
                temperature=0.0,
                max_tokens=600,
                context=f"llm-fallback:{result.paper_id}",
                expect_array=False,
            )

            # Extraer cada campo con valores por defecto
            brecha    = str(raw.get("brecha",    "")).strip()
            evolucion = str(raw.get("evolucion", "")).strip()
            senales   = raw.get("senales", [])
            modelos   = raw.get("modelos", [])
            metricas  = raw.get("metricas", {})
            limitaciones = str(raw.get("limitaciones", "")).strip()
            aplicacion   = str(raw.get("aplicacion", "")).strip()

            # Crear nuevo resultado combinando lo existente con lo nuevo
            improved = ExtractionResult(
                paper_id=     result.paper_id,
                ideas=        result.ideas,
                metodos=      result.metodos,
                resultados=   result.resultados,
                aplicaciones= result.aplicaciones,
                brecha=       brecha    or result.brecha,
                evolucion=    evolucion or result.evolucion,
                senales=      senales if senales else result.senales,
                modelos=      modelos if modelos else result.modelos,
                metricas=     metricas if metricas else result.metricas,
                limitaciones= limitaciones or result.limitaciones,
                aplicacion=   aplicacion or result.aplicacion,
            )
            cache.update(result.paper_id, improved)
            processed += 1
            writeLog("debug", logger,
                     f"[LLMFallbackEnricher] {result.paper_id}: "
                     f"brecha={bool(brecha)}, evolucion={bool(evolucion)}, "
                     f"senales={len(senales)}, modelos={len(modelos)}, "
                     f"metricas={len(metricas)}")

        writeLog("info", logger,
                 f"[LLMFallbackEnricher] Completed: {processed}/{len(candidates)} improved")
        return processed

    @staticmethod
    def _build_context(result: ExtractionResult) -> str:
        """Construye el texto de contexto para el LLM."""
        parts = []
        if result.ideas:
            parts.append(f"[Introduction]\n{result.ideas[:800]}")
        if result.metodos:
            parts.append(f"[Methods]\n{result.metodos[:800]}")
        if result.resultados:
            parts.append(f"[Results]\n{result.resultados[:800]}")
        if result.aplicaciones:
            parts.append(f"[Conclusion]\n{result.aplicaciones[:800]}")
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Reporter de calidad (diagnóstico) AMPLIADO
# ---------------------------------------------------------------------------

class ExtractionQualityReporter:
    ALL_DIMS = ("ideas", "metodos", "resultados", "aplicaciones",
                "brecha", "evolucion", "senales", "modelos", "metricas",
                "limitaciones", "aplicacion")
    SECTION_DIMS = ("ideas", "metodos", "resultados", "aplicaciones")
    PATTERN_DIMS = ("brecha", "evolucion")
    STRUCTURED_DIMS = ("senales", "modelos", "metricas", "limitaciones", "aplicacion")

    def __init__(self, config: EnrichmentConfig) -> None:
        self._cfg = config

    def coverage_ratio(self, results: List[ExtractionResult], dim: str) -> float:
        if not results:
            return 0.0
        if dim in ("senales", "modelos"):
            return sum(1 for r in results if getattr(r, dim)) / len(results)
        if dim == "metricas":
            return sum(1 for r in results if r.metricas) / len(results)
        return sum(1 for r in results if getattr(r, dim)) / len(results)

    def needs_llm_fallback(self, results: List[ExtractionResult]) -> bool:
        """True si alguna dimensión estructural cae por debajo del umbral."""
        for dim in self.PATTERN_DIMS + self.STRUCTURED_DIMS:
            if self.coverage_ratio(results, dim) < self._cfg.llm_fallback_threshold:
                return True
        return False

    def report(
        self,
        enriched_data: List[Dict],
        cache: ExtractionCache,
        llm_papers_processed: int = 0,
    ) -> None:
        results = cache.all_results()
        n_papers = len(results)
        if n_papers == 0:
            writeLog("warning", logger, "[QualityReport] No papers procesados")
            return

        sep = "=" * 70
        writeLog("info", logger, f"\n{sep}")
        writeLog("info", logger, "DIAGNÓSTICO DE CALIDAD — enrich_evidences")
        writeLog("info", logger, sep)

        cache.log_stats()
        if llm_papers_processed > 0:
            writeLog("info", logger,
                     f"[LLM fallback] {llm_papers_processed} papers mejorados por LLM")

        # Secciones fijas
        writeLog("info", logger,
                 f"\nSecciones fijas (dict lookup) — {n_papers} papers:")
        for dim in self.SECTION_DIMS:
            self._log_dim(dim, results, n_papers)

        # Dimensiones por patrón
        writeLog("info", logger, "\nDimensiones extraídas por regex + LLM fallback:")
        for dim in self.PATTERN_DIMS:
            self._log_dim(dim, results, n_papers)

        # Nuevas dimensiones estructuradas
        writeLog("info", logger, "\nNuevas dimensiones estructuradas (regex + LLM):")
        for dim in self.STRUCTURED_DIMS:
            ratio = self.coverage_ratio(results, dim)
            self._log_dim(dim, results, n_papers)
            if ratio < self._cfg.llm_fallback_threshold and not self._cfg.llm_fallback_enabled:
                writeLog("warning", logger,
                         f"  ⚠  Cobertura baja en '{dim}' ({ratio*100:.0f}%) y "
                         f"llm_fallback_enabled=false. Considerar activarlo.")

        # Distribución de evidencias (con nuevos campos)
        total_ev = enriched_empty = enriched_rich = enriched_partial = 0
        for concept in enriched_data:
            for cluster in concept.get("clusters", []):
                for ev in cluster.get("evidences", []):
                    total_ev += 1
                    se = ev.get("structured_evidence", {})
                    n = len(se)
                    if n == 0:
                        enriched_empty += 1
                    elif n >= 6:  # 6 campos básicos (ideas, metodos, resultados, aplicaciones, brecha, evolucion) + al menos uno nuevo
                        enriched_rich += 1
                    else:
                        enriched_partial += 1

        if total_ev > 0:
            writeLog("info", logger, f"\nDistribución de evidencias ({total_ev} total):")
            writeLog("info", logger,
                     f"  Ricas  (≥6 campos): {enriched_rich}  ({enriched_rich/total_ev*100:.0f}%)")
            writeLog("info", logger,
                     f"  Parcia (1-5):     {enriched_partial} ({enriched_partial/total_ev*100:.0f}%)")
            writeLog("info", logger,
                     f"  Vacías (0):       {enriched_empty} ({enriched_empty/total_ev*100:.0f}%)")

            empty_ratio = enriched_empty / total_ev
            if empty_ratio > 0.30:
                writeLog("warning", logger,
                         f"\n  ⚠  {empty_ratio*100:.0f}% de evidencias sin structured_evidence. "
                         f"Verificar que papers_text.json tiene 'clean_sections' poblado "
                         f"y revisar 'enrichment.section_map' en studyDescription.json.")

        writeLog("info", logger, sep)

    @staticmethod
    def _log_dim(dim: str, results: List[ExtractionResult], n_papers: int) -> None:
        count = sum(1 for r in results if getattr(r, dim))
        pct   = count / n_papers * 100
        bar   = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        status = "✓" if pct >= 70 else ("⚠" if pct >= 40 else "✗")
        writeLog("info", logger,
                 f"  {status} {dim:15s}: {count:3d}/{n_papers} ({pct:4.0f}%) {bar}")


# ---------------------------------------------------------------------------
# Pipeline de enriquecimiento (actualizado)
# ---------------------------------------------------------------------------

class EnrichmentPipeline:
    def __init__(
        self,
        config: Optional[EnrichmentConfig] = None,
        reporter: Optional[ExtractionQualityReporter] = None,
    ) -> None:
        self._cfg      = config or EnrichmentConfig()
        self._extractor = PaperSectionExtractor(self._cfg)
        self._cache    = ExtractionCache(self._extractor)
        self._fallback = LLMFallbackEnricher(self._cfg)
        self._reporter = reporter or ExtractionQualityReporter(self._cfg)

    def run(self, clustered_data: List[Dict], papers_text: Dict) -> List[Dict]:
        total_ev = sum(
            len(cluster.get("evidences", []))
            for concept in clustered_data
            for cluster in concept.get("clusters", [])
        )
        n_unique = len({
            ev.get("doc_id")
            for concept in clustered_data
            for cluster in concept.get("clusters", [])
            for ev in cluster.get("evidences", [])
        })
        writeLog("info", logger,
                 f"[EnrichmentPipeline] Pasada 1 (regex): "
                 f"{total_ev} evidencias, {n_unique} papers únicos, "
                 f"{len(clustered_data)} conceptos")

        # Pasada 1: extracción regex para todos los papers
        enriched = [self._enrich_concept(c, papers_text) for c in clustered_data]

        # Pasada 2: LLM fallback (condicional)
        llm_processed = 0
        if self._cfg.llm_fallback_enabled:
            all_results = self._cache.all_results()
            if self._reporter.needs_llm_fallback(all_results):
                writeLog("info", logger,
                         "[EnrichmentPipeline] Pasada 2 (LLM fallback): "
                         "cobertura insuficiente en campos estructurados")
                llm_processed = self._fallback.enrich_missing(self._cache, papers_text, self._cfg)
                # Re-aplicar caché actualizado
                enriched = self._reapply_cache(enriched)
            else:
                writeLog("info", logger,
                         "[EnrichmentPipeline] Pasada 2 (LLM fallback): omitida "
                         f"(cobertura >= {self._cfg.llm_fallback_threshold*100:.0f}%)")
        else:
            writeLog("info", logger,
                     "[EnrichmentPipeline] LLM fallback desactivado en config")

        self._reporter.report(enriched, self._cache, llm_processed)
        return enriched

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _enrich_concept(self, concept: Dict, papers_text: Dict) -> Dict:
        return {
            **concept,
            "clusters": [self._enrich_cluster(cl, papers_text)
                         for cl in concept.get("clusters", [])],
        }

    def _enrich_cluster(self, cluster: Dict, papers_text: Dict) -> Dict:
        doc_title_map = dict(zip(
            cluster.get("papers", []),
            cluster.get("paper_titles", []),
        ))
        return {
            **cluster,
            "evidences": [
                self._enrich_evidence(ev, papers_text, doc_title_map)
                for ev in cluster.get("evidences", [])
            ],
        }

    def _enrich_evidence(
        self, evidence: Dict, papers_text: Dict, doc_title_map: Dict
    ) -> Dict:
        doc_id = evidence.get("doc_id", "")
        result = self._cache.get(doc_id, papers_text)
        enriched = {**evidence, "structured_evidence": result.to_dict()}
        if not enriched.get("paper_title") or enriched["paper_title"] == "Unknown":
            enriched["paper_title"] = doc_title_map.get(doc_id, "Unknown")
        return enriched

    def _reapply_cache(self, enriched_data: List[Dict]) -> List[Dict]:
        """Reconstruye structured_evidence con el caché actualizado."""
        updated = []
        for concept in enriched_data:
            updated_clusters = []
            for cluster in concept.get("clusters", []):
                updated_evidences = []
                for ev in cluster.get("evidences", []):
                    doc_id = ev.get("doc_id", "")
                    if doc_id in self._cache._cache:
                        result = self._cache._cache[doc_id]
                        ev = {**ev, "structured_evidence": result.to_dict()}
                    updated_evidences.append(ev)
                updated_clusters.append({**cluster, "evidences": updated_evidences})
            updated.append({**concept, "clusters": updated_clusters})
        return updated

# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def processEnrichEvidences() -> Optional[List[Dict]]:
    """Orquesta I/O + pipeline."""
    input_dir, output_dir = inicioModulo("processEnrichEvidences")

    cfg = EnrichmentConfig.from_file(input_dir / "studyDescription.json")
    cfg.log_active_config()

    clustered_data = read_json(output_dir / "clustered_evidences.json")
    papers_data = read_json(output_dir / "papers_text.json")

    papers_text = {
        p["paper_id"]: p
        for p in papers_data
        if "paper_id" in p
    }
    writeLog("info", logger,
             f"[EnrichEvidences] Cargados: {len(clustered_data)} conceptos, "
             f"{len(papers_text)} papers")

    pipeline = EnrichmentPipeline(config=cfg)
    enriched_data = pipeline.run(
        clustered_data=clustered_data,
        papers_text=papers_text,
    )

    write_json(output_dir / "enriched_evidences.json", enriched_data)

    return enriched_data


if __name__ == "__main__":
    processEnrichEvidences()