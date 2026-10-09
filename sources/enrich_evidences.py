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
  3. El LLM es fallback opcional cuando la cobertura regex es insuficiente
     (configurable por umbral).

CLASIFICACIÓN DE CAMPOS:
  - SECCIONES TEXTUALES (texto largo, resumen fiel de una sección real del paper):
      ideas        → contexto / problema abordado (Introduction/Background)
      methods      → cómo lo resuelve (Methods/Methodology)
      results      → qué obtuvo (Results/Findings)
      applications → qué implica y concluye (Discussion/Conclusion, sección completa)

  - METADATOS / ETIQUETAS (extractos cortos, conceptos que pueden aparecer
    en CUALQUIER parte del paper, no atados a una sección concreta):
      gap          → gap de investigación identificado
      evolutions   → trabajos futuros propuestos
      signals      → lista de señales fisiológicas/comportamentales usadas
      models       → lista de modelos de ML/DL/NLP empleados
      metrics      → diccionario con métricas de rendimiento
      limitations  → texto con limitaciones del estudio
      usage        → dominio/contexto de uso o aplicación (frase corta)
      solution     → contribución principal o solución propuesta (1-2 frases)

NOTA IMPORTANTE SOBRE EL RENOMBRADO "applications" -> "usage":
  Anteriormente existían DOS conceptos distintos compartiendo el mismo nombre
  en inglés ("applications" / "aplicacion"):
    1) La sección textual completa de Discusión/Conclusión del paper.
    2) Un metadato corto con el dominio o caso de uso (p.ej. "conducción",
       "salud mental", "videojuegos").
  Al traducir las etiquetas originales en español ("aplicaciones" para la
  sección y "aplicacion" para el metadato) ambas quedaron mapeadas a la
  misma clave en inglés ("applications"), lo que provocaba que, en
  `ExtractionResult.to_dict()`, el metadato corto SOBRESCRIBIERA el texto
  largo de la sección (mismo key, último en escribirse gana). Para eliminar
  esta ambigüedad, el metadato corto se renombra a `usage`. La sección
  textual conserva el nombre `applications`.

  ⚠ Si `trainingMaterials.py` (u otro consumidor de `structured_evidence`)
  leía el campo "applications" esperando el metadato corto, debe
  actualizarse para leer "usage" en su lugar.

Configuración en studyDescription.json:
    "enrichment": {
        "max_section_length": 1500,
        "max_extracted_sentences": 3,
        "llm_fallback_threshold": 0.40,
        "llm_fallback_enabled": true,
        "section_map": {
            "ideas":        ["introduction", "background", "abstract"],
            "methods":      ["methods", "methodology", "experimental", "procedure"],
            "results":      ["results", "findings", "evaluation"],
            "applications": ["discussion", "conclusion", "application"]
        },
        "gap_search_in":    ["ideas"],
        "future_search_in": ["applications", "results"],
        "signals_list": ["ECG", "EEG", "EDA", "GSR", "EMG", "EOG", "fNIRS", "HRV", "PPG", "pupillometry", "eye tracking", "respiration"],
        "models_list": ["SVM", "KNN", "Random Forest", "XGBoost", "CNN", "LSTM", "BERT", "Transformer", "ViT", "TCN", "GNN", "DBN", "GAN"]
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set, Any

from sources.common.common import logger, writeLog
from sources.common.utils import inicioModulo, read_json, write_json


# ===========================================================================
# PATRONES REGEX PARA EXTRACCIÓN DE METADATOS
# ===========================================================================

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

# Patrones para extraer la contribución/solución principal
_SOLUTION_PATTERNS: List[re.Pattern] = [
    re.compile(
        r'(?:proponemos|we propose|presentamos|we present|'
        r'introducimos|we introduce|desarrollamos|we develop|'
        r'sugerimos|we suggest|demostramos|we demonstrate|'
        r'este trabajo (?:presenta|propone)|this work (?:presents|proposes))'
        r'[^.]{10,150}[.]',
        re.IGNORECASE | re.DOTALL),
    re.compile(
        r'(?:el objetivo(?: principal)?|the (?:main )?goal|'
        r'nuestro enfoque|our approach|'
        r'la contribuci[oó]n(?: principal)?|the (?:main )?contribution)[^.]{10,150}[.]',
        re.IGNORECASE | re.DOTALL),
    re.compile(
        r'(?:en este trabajo|in this paper|'
        r'este estudio|this study|'
        r'aqu[ií] presentamos|here we present)[^.]{10,150}[.]',
        re.IGNORECASE | re.DOTALL),
]

# Señales fisiológicas/comportamentales (lista por defecto)
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

# Modelos de ML/DL/NLP (lista por defecto)
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

# Patrón para métricas (accuracy, precision, recall, F1, etc.)
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

# Frases para "usage" (metadato corto: dominio/contexto de uso o aplicación)
_USAGE_PHRASES = re.compile(
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
    words = re.findall(r'[a-zA-Z\-]+', text.lower())
    for w in words:
        if w in signal_set or any(s in w for s in signal_set):
            for canonical in _DEFAULT_SIGNALS:
                if canonical.lower() == w or canonical.lower() in w:
                    found.add(canonical)
                    break
            else:
                found.add(w)
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
    return sorted(found)


def _extract_metrics(text: str) -> Dict[str, str]:
    """Extrae métricas de rendimiento (accuracy, precision, F1, etc.) como dict."""
    metrics = {}
    for match in _METRIC_PATTERN.finditer(text):
        key = match.group(0).strip().split()[0]
        value = match.group(1) + "%" if match.group(1) else ""
        if key.lower() not in metrics:
            metrics[key.lower()] = value
    return metrics


def _extract_limitations(text: str, max_sentences: int = 2) -> str:
    """Extrae frases sobre limitaciones del estudio."""
    sentences = re.split(r'[.!?]+', text)
    limitations = []
    for sent in sentences:
        if _LIMITATION_PHRASES.search(sent):
            limitations.append(sent.strip())
            if len(limitations) >= max_sentences:
                break
    return ". ".join(limitations)


def _extract_usage(text: str, max_sentences: int = 2) -> str:
    """
    Extrae frases sobre el dominio/contexto de uso (metadato corto, NO la
    sección textual de Discusión/Conclusión). Busca en TODO el texto
    combinado del paper, no en una sección concreta.
    """
    sentences = re.split(r'[.!?]+', text)
    usages = []
    for sent in sentences:
        if _USAGE_PHRASES.search(sent):
            usages.append(sent.strip())
            if len(usages) >= max_sentences:
                break
    return ". ".join(usages)


def _extract_solution(text: str, max_sentences: int = 2) -> str:
    """Extrae la contribución principal o solución propuesta (metadato corto)."""
    return _extract_by_pattern(text, _SOLUTION_PATTERNS, max_sentences)


# ===========================================================================
# CONFIGURACIÓN
# ===========================================================================

_DEFAULT_SECTION_MAP: Dict[str, List[str]] = {
    "ideas":        ["introduction", "background", "abstract"],
    "methods":      ["methods", "methodology", "experimental", "procedure"],
    "results":      ["results", "findings", "evaluation"],
    "applications": ["discussion", "conclusion", "application"],
}


@dataclass
class EnrichmentConfig:
    """
    Parámetros de enriquecimiento. Todos configurables desde studyDescription.json.
    """
    max_section_length:      int   = 1500
    max_extracted_sentences: int   = 3
    llm_fallback_threshold:  float = 0.40
    llm_fallback_enabled:    bool  = True

    section_map: Dict[str, List[str]] = field(
        default_factory=lambda: dict(_DEFAULT_SECTION_MAP)
    )
    gap_search_in:    List[str] = field(default_factory=lambda: ["ideas"])
    # NOTA: las claves deben coincidir con las de `section_map` (en inglés).
    future_search_in: List[str] = field(default_factory=lambda: ["applications", "results"])

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


# ===========================================================================
# RESULTADO DE EXTRACCIÓN (MODELO DE DATOS)
# ===========================================================================

@dataclass(frozen=True)
class ExtractionResult:
    """
    Resultado de extraer las dimensiones de un paper.

    CLASIFICACIÓN DE CAMPOS:
      - SECCIONES TEXTUALES (texto largo, resumen fiel de una sección real):
          ideas, methods, results, applications
      - METADATOS / ETIQUETAS (extractos cortos, conceptos que pueden
        provenir de cualquier parte del paper):
          gap, evolutions, signals, models, metrics, limitations, usage, solution

    `applications` (sección textual) y `usage` (metadato corto) son
    conceptos DISTINTOS y deliberadamente no comparten nombre — ver nota
    en el docstring del módulo sobre el bug histórico de colisión.

    Los textos se almacenan en el idioma ORIGINAL del paper (sin traducir).
    Las traducciones ocurren en trainingMaterials.py en el momento de renderizado.
    """
    paper_id: str

    # --- SECCIONES TEXTUALES ---
    ideas:        str = ""   # Introducción/Background
    methods:      str = ""   # Methods/Methodology
    results:      str = ""   # Results/Findings
    applications: str = ""   # Discussion/Conclusion (sección completa)

    # --- METADATOS / ETIQUETAS ---
    gap:         str = ""               # gap de investigación
    evolutions:  str = ""               # trabajos futuros
    signals:     List[str] = field(default_factory=list)      # señales usadas
    models:      List[str] = field(default_factory=list)      # modelos empleados
    metrics:     Dict[str, str] = field(default_factory=dict) # métricas de rendimiento
    limitations: str = ""               # limitaciones
    usage:       str = ""               # dominio/contexto de uso (renombrado desde "aplicacion")
    solution:    str = ""               # contribución principal / solución propuesta
    differential_contribution: str = ""  # aporte singular/diferencial del paper

    # ------------------------------------------------------------------
    # Propiedades de agrupación
    # ------------------------------------------------------------------

    @property
    def sections(self) -> Dict[str, str]:
        """Devuelve solo las secciones textuales (narrativa larga)."""
        return {
            "ideas": self.ideas,
            "methods": self.methods,
            "results": self.results,
            "applications": self.applications,
        }

    @property
    def metadata(self) -> Dict[str, Any]:
        """Devuelve solo los metadatos/etiquetas (estructurados)."""
        return {
            "gap": self.gap,
            "evolutions": self.evolutions,
            "signals": self.signals,
            "models": self.models,
            "metrics": self.metrics,
            "limitations": self.limitations,
            "usage": self.usage,
            "solution": self.solution,
            "differential_contribution": self.differential_contribution,
        }

    @property
    def is_empty(self) -> bool:
        """Indica si no se ha extraído absolutamente nada."""
        return not any([
            self.ideas, self.methods, self.results, self.applications,
            self.gap, self.evolutions,
            self.signals, self.models, self.metrics,
            self.limitations, self.usage, self.solution,
            self.differential_contribution,
        ])

    @property
    def section_coverage(self) -> Dict[str, bool]:
        """Devuelve un diccionario indicando qué campos tienen contenido."""
        return {
            "ideas":        bool(self.ideas),
            "methods":      bool(self.methods),
            "results":      bool(self.results),
            "applications": bool(self.applications),
            "gap":          bool(self.gap),
            "evolutions":   bool(self.evolutions),
            "signals":      bool(self.signals),
            "models":       bool(self.models),
            "metrics":      bool(self.metrics),
            "limitations":  bool(self.limitations),
            "usage":        bool(self.usage),
            "solution":     bool(self.solution),
            "differential_contribution": bool(self.differential_contribution),
        }

    @property
    def n_populated(self) -> int:
        """Número de campos poblados (sobre 13 totales)."""
        return sum(self.section_coverage.values())

    def needs_llm_fallback(self, config: EnrichmentConfig) -> bool:
        """
        True si alguno de los campos considerados 'estructurales' está vacío.
        Se usan gap, evolutions, signals, models, limitations, usage, solution.
        """
        return not (self.gap and self.evolutions and self.signals and self.models
                    and self.limitations and self.usage and self.solution)

    def to_dict(self) -> Dict:
        """
        Serializa para structured_evidence en el JSON de salida.
        Incluye TODOS los campos con contenido (tanto secciones como metadatos).
        `sections` y `metadata` ya no comparten ninguna clave, por lo que
        no hay riesgo de que un metadato sobrescriba una sección textual.
        """
        result = {}
        # Secciones textuales
        for key, val in self.sections.items():
            if val:
                result[key] = val
        # Metadatos (con manejo especial para listas y dicts)
        for key, val in self.metadata.items():
            if val:
                result[key] = val
        return result


# ===========================================================================
# EXTRACTOR PRINCIPAL (STATELESS)
# ===========================================================================

class PaperSectionExtractor:
    """
    Extrae todas las dimensiones de un paper. Stateless.

    Tecnología:
      - Secciones textuales: dict lookup O(1) sobre clean_sections.
      - Metadatos (gap, evolutions, solution): regex bilingüe sobre secciones candidatas.
      - Metadatos (signals, models, metrics, limitations, usage):
          regex específicos sobre el texto combinado del paper.
    """

    def __init__(self, config: EnrichmentConfig) -> None:
        self._cfg = config
        self._signal_set = {s.lower() for s in config.signals_list}
        self._model_set = {m.lower() for m in config.models_list}

    def extract(self, paper_id: str, paper: Optional[Dict]) -> ExtractionResult:
        if not paper:
            return ExtractionResult(paper_id=paper_id)

        raw_sections = paper.get("clean_sections", {})
        section_texts: Dict[str, str] = {}

        # 1. Extraer secciones textuales
        for key, candidates in self._cfg.section_map.items():
            text = next((raw_sections[c] for c in candidates if raw_sections.get(c)), "")
            if not text and key == "ideas":
                text = paper.get("abstract", "") or paper.get("clean_text", "")[:2000]
            section_texts[key] = text[: self._cfg.max_section_length] if text else ""

        # 2. Extraer metadatos (gap, trabajo futuro y solución) desde secciones candidatas
        gap = self._extract_gap(section_texts)
        evolutions = self._extract_future_work(section_texts)
        solution = self._extract_solution(section_texts)

        # 3. Extraer metadatos estructurados desde la totalidad del texto combinado
        combined_text = "\n".join(section_texts.values()) + "\n" + paper.get("clean_text", "")[:3000]
        signals = _extract_signals(combined_text, self._signal_set)
        models = _extract_models(combined_text, self._model_set)
        metrics = _extract_metrics(combined_text)
        limitations = _extract_limitations(combined_text, self._cfg.max_extracted_sentences)
        usage = _extract_usage(combined_text, self._cfg.max_extracted_sentences)

        return ExtractionResult(
            paper_id=paper_id,
            ideas=section_texts.get("ideas", ""),
            methods=section_texts.get("methods", ""),
            results=section_texts.get("results", ""),
            applications=section_texts.get("applications", ""),
            gap=gap,
            evolutions=evolutions,
            signals=signals,
            models=models,
            metrics=metrics,
            limitations=limitations,
            usage=usage,
            solution=solution,
        )

    # ------------------------------------------------------------------
    # Métodos de extracción específicos para metadatos
    # ------------------------------------------------------------------

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

    def _extract_solution(self, section_texts: Dict[str, str]) -> str:
        # Buscar en ideas (introducción) y resultados (donde suele aparecer la contribución)
        for key in ["ideas", "results"]:
            text = section_texts.get(key, "")
            if text:
                result = _extract_by_pattern(text, _SOLUTION_PATTERNS,
                                             self._cfg.max_extracted_sentences)
                if result:
                    return result
        return ""


# ===========================================================================
# CACHÉ DE EXTRACCIONES
# ===========================================================================

class ExtractionCache:
    """
    Cachea ExtractionResult por paper_id para evitar reprocesar
    el mismo paper en múltiples clusters.
    """

    def __init__(self, extractor: PaperSectionExtractor) -> None:
        self._extractor = extractor
        self._cache: Dict[str, ExtractionResult] = {}
        self.hits = 0
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


# ===========================================================================
# LLM FALLBACK (SEGUNDA PASADA)
# ===========================================================================

class LLMFallbackEnricher:
    """
    Usa el LLM federado para extraer TODOS los campos estructurados
    en los papers donde la cobertura regex sea insuficiente.
    """

    _SYSTEM_PROMPT = (
        "You are a structured information extractor for academic papers. "
        "Extract ONLY what is explicitly stated in the provided text. "
        "Do not infer, summarize, or add external knowledge. "
        "Prioritize what is SPECIFIC and DIFFERENTIAL to this paper. "
        "Ignore generic background/problem statements that apply to the whole "
        "research field (they are already known and repeat across papers). "
        "Return a JSON object with these keys:\n"
        "  'gap'         : research gap identified (1-3 sentences, original language)\n"
        "  'evolutions'  : future work proposed (1-3 sentences, original language)\n"
        "  'signals'     : list of physiological/behavioral signals used (e.g., ['ECG','EEG'])\n"
        "  'models'      : list of machine learning models used (e.g., ['SVM','LSTM'])\n"
        "  'metrics'     : dict with performance metrics (e.g., {'accuracy':'89.9%'})\n"
        "  'limitations' : limitations of the study (1-2 sentences, original language)\n"
        "  'usage'       : application domain or use case context (1-2 sentences, original language)\n"
        "  'solution'    : main contribution or proposed solution (1-2 sentences, original language)\n"
        "  'differential_contribution': what this paper uniquely adds versus the "
        "state of the art (1-2 sentences, original language); empty if not stated\n"
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
                f"Focus on what is specific/differential to this paper and skip "
                f"generic background shared across the field.\n"
                f"Return valid JSON with keys: gap, evolutions, signals, models, "
                f"metrics, limitations, usage, solution, differential_contribution.\n\n"
                f"Text:\n{context_text}\n\n"
                f'Return JSON: {{"gap": "...", "evolutions": "...", '
                f'"signals": [...], "models": [...], '
                f'"metrics": {{...}}, "limitations": "...", '
                f'"usage": "...", "solution": "...", '
                f'"differential_contribution": "..."}}'
            )

            raw = llm.generate_json(
                prompt=prompt,
                system_prompt=self._SYSTEM_PROMPT,
                temperature=0.0,
                max_tokens=600,
                context=f"llm-fallback:{result.paper_id}",
                expect_array=False,
            )

            gap = str(raw.get("gap", "")).strip()
            evolutions = str(raw.get("evolutions", "")).strip()
            signals = raw.get("signals", [])
            models = raw.get("models", [])
            metrics = raw.get("metrics", {})
            limitations = str(raw.get("limitations", "")).strip()
            usage = str(raw.get("usage", "")).strip()
            solution = str(raw.get("solution", "")).strip()
            differential = str(raw.get("differential_contribution", "")).strip()

            improved = ExtractionResult(
                paper_id=result.paper_id,
                ideas=result.ideas,
                methods=result.methods,
                results=result.results,
                applications=result.applications,
                gap=gap or result.gap,
                evolutions=evolutions or result.evolutions,
                signals=signals if signals else result.signals,
                models=models if models else result.models,
                metrics=metrics if metrics else result.metrics,
                limitations=limitations or result.limitations,
                usage=usage or result.usage,
                solution=solution or result.solution,
                differential_contribution=differential or result.differential_contribution,
            )
            cache.update(result.paper_id, improved)
            processed += 1
            writeLog("debug", logger,
                     f"[LLMFallbackEnricher] {result.paper_id}: "
                     f"gap={bool(gap)}, evolutions={bool(evolutions)}, "
                     f"signals={len(signals)}, models={len(models)}, "
                     f"metrics={len(metrics)}, usage={bool(usage)}, solution={bool(solution)}")

        writeLog("info", logger,
                 f"[LLMFallbackEnricher] Completed: {processed}/{len(candidates)} improved")
        return processed

    @staticmethod
    def _build_context(result: ExtractionResult) -> str:
        """Construye el texto de contexto para el LLM usando las secciones textuales."""
        parts = []
        if result.ideas:
            parts.append(f"[Introduction]\n{result.ideas[:800]}")
        if result.methods:
            parts.append(f"[Methods]\n{result.methods[:800]}")
        if result.results:
            parts.append(f"[Results]\n{result.results[:800]}")
        if result.applications:
            parts.append(f"[Conclusion]\n{result.applications[:800]}")
        return "\n\n".join(parts)


# ===========================================================================
# REPORTER DE CALIDAD
# ===========================================================================

class ExtractionQualityReporter:
    """
    Reporta la cobertura de extracción por dimensión, separando
    secciones textuales y metadatos.
    """

    SECTION_DIMS = ("ideas", "methods", "results", "applications")
    METADATA_DIMS = ("gap", "evolutions", "signals", "models", "metrics",
                     "limitations", "usage", "solution")
    ALL_DIMS = SECTION_DIMS + METADATA_DIMS

    def __init__(self, config: EnrichmentConfig) -> None:
        self._cfg = config

    def coverage_ratio(self, results: List[ExtractionResult], dim: str) -> float:
        if not results:
            return 0.0
        return sum(1 for r in results if getattr(r, dim)) / len(results)

    def needs_llm_fallback(self, results: List[ExtractionResult]) -> bool:
        """True si algún metadato estructural cae por debajo del umbral."""
        for dim in self.METADATA_DIMS:
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

        # Secciones textuales
        writeLog("info", logger,
                 f"\n📄 SECCIONES TEXTUALES (extraídas por dict lookup) — {n_papers} papers:")
        for dim in self.SECTION_DIMS:
            self._log_dim(dim, results, n_papers)

        # Metadatos
        writeLog("info", logger,
                 f"\n🏷️  METADATOS (extraídos por regex + LLM fallback) — {n_papers} papers:")
        for dim in self.METADATA_DIMS:
            ratio = self.coverage_ratio(results, dim)
            self._log_dim(dim, results, n_papers)
            if ratio < self._cfg.llm_fallback_threshold and not self._cfg.llm_fallback_enabled:
                writeLog("warning", logger,
                         f"  ⚠  Cobertura baja en '{dim}' ({ratio*100:.0f}%) y "
                         f"llm_fallback_enabled=false. Considerar activarlo.")

        # Distribución de evidencias (cuántos campos tienen contenido)
        total_ev = enriched_empty = enriched_rich = enriched_partial = 0
        for concept in enriched_data:
            for cluster in concept.get("clusters", []):
                for ev in cluster.get("evidences", []):
                    total_ev += 1
                    se = ev.get("structured_evidence", {})
                    n = len(se)
                    if n == 0:
                        enriched_empty += 1
                    elif n >= 6:  # al menos 6 campos poblados
                        enriched_rich += 1
                    else:
                        enriched_partial += 1

        if total_ev > 0:
            writeLog("info", logger, f"\n📊 Distribución de evidencias ({total_ev} total):")
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
        pct = count / n_papers * 100
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        status = "✓" if pct >= 70 else ("⚠" if pct >= 40 else "✗")
        writeLog("info", logger,
                 f"  {status} {dim:15s}: {count:3d}/{n_papers} ({pct:4.0f}%) {bar}")


# ===========================================================================
# PIPELINE DE ENRIQUECIMIENTO
# ===========================================================================

class EnrichmentPipeline:
    """
    Orquesta la extracción en dos pasadas:
      Pasada 1: PaperSectionExtractor (regex + dict lookup) — todos los papers
      Pasada 2: LLMFallbackEnricher   — solo papers con baja cobertura de metadatos
    """

    def __init__(
        self,
        config: Optional[EnrichmentConfig] = None,
        reporter: Optional[ExtractionQualityReporter] = None,
    ) -> None:
        self._cfg = config or EnrichmentConfig()
        self._extractor = PaperSectionExtractor(self._cfg)
        self._cache = ExtractionCache(self._extractor)
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
                         "cobertura insuficiente en metadatos")
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


# ===========================================================================
# ENTRY POINT
# ===========================================================================

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