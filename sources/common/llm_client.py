"""
llm_client.py
=============
Cliente federado para modelos LLM.

Arquitectura en capas (SOA):
  ┌─────────────────────────────────────────┐
  │           LLMClient (fachada)           │  ← punto de entrada único
  ├─────────────────────────────────────────┤
  │  RetryPolicy │ PromptBuilder │ ResponseParser │
  ├─────────────────────────────────────────┤
  │         LLMTransport (interfaz)         │
  │   OllamaTransport │ FallbackTransport   │  ← proveedores intercambiables
  └─────────────────────────────────────────┘

Principios aplicados:
  - SRP : cada clase tiene una responsabilidad
  - OCP : nuevos proveedores sin tocar LLMClient
  - DIP : LLMClient depende de la abstracción LLMTransport, no de ollama
  - Thread-safe : sin estado global mutable

Gestión del modo thinking (Qwen3, DeepSeek-R1, etc.):
  Qwen3 puede emitir el razonamiento de TRES formas distintas según la
  versión de Ollama y la configuración del modelo:

    Forma A – Campo separado 'thinking' en el dict de respuesta
              (Ollama >= 0.6, opción think:true activa).
              → OllamaTransport: ignorar ese campo; usar sólo 'response'.

    Forma B – Bloque 💭…🤔 al inicio del campo 'response'
              (versiones antiguas de Ollama o modelos sin soporte nativo).
              → OllamaTransport: regex _THINK_BLOCK lo elimina (reactivo).

    Forma C – Texto plano de razonamiento SIN etiquetas, antes de la
              respuesta real, separado por una línea en blanco o por un
              marcador como "Okay, let's tackle" / "I need to answer".
              ES LA CAUSA DEL BUG ACTUAL: ni /no_think ni el regex de
              💭 lo capturan porque no hay etiquetas.
              → ResponseParser: _strip_untagged_thinking() detecta bloques
                de prosa en inglés antes del contenido en español esperado
                y los elimina.

  Defensa en profundidad (5 capas):
    0. Nativa     : think=False vía API de Ollama, si ollama-python lo
                    soporta (detectado en runtime con _ollama_supports_think).
                    Es el mecanismo MÁS FIABLE: actúa a nivel de motor y no
                    depende de que la plantilla de chat del modelo reconozca
                    ningún token especial. Si la librería instalada es
                    antigua, se omite y se cae a la capa 1.
    1. Proactiva  : /no_think en el system prompt (OllamaTransport).
                    Capa de refuerzo / compatibilidad hacia atrás; algunas
                    plantillas de Qwen3 solo reconocen este token en el
                    turno de USUARIO, no en system, por lo que NO debe ser
                    el único mecanismo de desactivación.
    2. Reactiva A : eliminar campo 'thinking' del dict (OllamaTransport)
    3. Reactiva B : regex 💭…🤔 (OllamaTransport + ResponseParser)
    4. Reactiva C : detección de prosa de razonamiento sin etiquetas
                    (ResponseParser._strip_untagged_thinking)
"""

from __future__ import annotations

import inspect
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Type, TypeVar, Union

import ollama
from pydantic import BaseModel, ValidationError

from sources.common.common import logger, writeLog, processControl

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Utilidad de configuración global
# ---------------------------------------------------------------------------

def _get_llm_defaults() -> Dict[str, Any]:
    """Extrae la configuración por defecto de LLM desde processControl."""
    return getattr(processControl, 'defaults', {}).get("llm", {})


def _ollama_supports_think() -> bool:
    """
    Detecta en runtime si la versión instalada de ollama-python acepta el
    parámetro nativo `think` en generate(). Introducido en ollama-python
    ~0.4.x junto con el soporte de "thinking models" de Ollama server >= 0.6.

    Se evalúa una sola vez (módulo cacheado) para no penalizar cada llamada.
    Si la librería instalada es más antigua, devolvemos False y el
    transporte cae de vuelta al único mecanismo disponible: el prefijo
    textual `/no_think`.
    """
    try:
        sig = inspect.signature(ollama.generate)
        return "think" in sig.parameters
    except (TypeError, ValueError):
        return False


_SUPPORTS_NATIVE_THINK = _ollama_supports_think()


# ---------------------------------------------------------------------------
# Capa 1 – Transporte
# ---------------------------------------------------------------------------

class LLMTransport(ABC):
    @abstractmethod
    def complete(self, prompt: str, system: Optional[str], options: Dict[str, Any]) -> str:
        """Envía una petición y devuelve el texto crudo de la respuesta."""


class OllamaTransport(LLMTransport):
    """
    Implementación concreta sobre Ollama.
    Gestiona las Formas A y B del thinking (ver docstring del módulo).
    La Forma C (prosa sin etiquetas) se gestiona en ResponseParser.

    Desactivación del thinking — dos mecanismos en capas:
      1. NATIVO (preferente): parámetro `think=False` de la API de Ollama
         (ollama-python >= 0.4.x). Desactiva el razonamiento a nivel de
         motor, independientemente de si la plantilla de chat del modelo
         reconoce o no el token `/no_think` en el texto. Se activa solo
         si `_ollama_supports_think()` lo detecta disponible.
      2. TEXTUAL (refuerzo / compatibilidad hacia atrás): prefijo
         `/no_think` en el system prompt. Se mantiene siempre como capa
         adicional — no estorba si el mecanismo nativo ya está activo,
         y es el único disponible en versiones antiguas de ollama-python
         o de Ollama server (< 0.6).
    """

    _NO_THINK_PREFIX = "/no_think"
    # Forma B: bloque etiquetado en el campo response
    _THINK_BLOCK = re.compile(r"💭.*?🤔\s*", re.DOTALL | re.IGNORECASE)

    def __init__(self, model: str, suppress_thinking: bool = True) -> None:
        self.model = model
        self.suppress_thinking = suppress_thinking
        self._use_native_think = suppress_thinking and _SUPPORTS_NATIVE_THINK
        if suppress_thinking and not _SUPPORTS_NATIVE_THINK:
            writeLog("warning", logger,
                     f"[OllamaTransport:{model}] ollama-python instalado no soporta "
                     f"el parámetro nativo 'think'. Usando solo el prefijo textual "
                     f"'/no_think' (menos fiable). Actualiza con: pip install --upgrade ollama")

    def complete(self, prompt: str, system: Optional[str], options: Dict[str, Any], keep_alive: Any = None) -> str:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "options": options,
        }
        if keep_alive is not None:
            kwargs["keep_alive"] = keep_alive
        effective_system = self._build_system(system)
        if effective_system:
            kwargs["system"] = effective_system
        if self._use_native_think:
            kwargs["think"] = False

        try:
            response = ollama.generate(**kwargs)
        except TypeError as exc:
            # Salvaguarda: si la detección de firma falló (p.ej. wrapper/mock
            # que no expone la firma real) y el server/cliente realmente no
            # soporta 'think', reintentamos una vez sin él en vez de fallar
            # toda la pasada de enriquecimiento.
            if "think" in kwargs:
                writeLog("warning", logger,
                         f"[OllamaTransport:{self.model}] 'think' rechazado en runtime "
                         f"({exc}); reintentando sin él.")
                kwargs.pop("think")
                response = ollama.generate(**kwargs)
            else:
                raise

        # Forma A: Ollama >= 0.6 con think activo — el razonamiento viene en
        # un campo separado 'thinking'. Ignorarlo; usar sólo 'response'.
        raw = response.get("response", "").strip()

        # Fallback: si 'response' está vacío Y suppress_thinking está OFF,
        # intentar extraer del campo 'thinking' (uso deliberado del razonamiento).
        if not raw and not self.suppress_thinking:
            raw = self._extract_from_thinking(response.get("thinking", ""))

        # Forma B: eliminar bloques 💭…🤔 residuales en 'response'
        if self.suppress_thinking and raw:
            raw = self._THINK_BLOCK.sub("", raw).strip()

        return raw

    def _build_system(self, system: Optional[str]) -> Optional[str]:
        if not self.suppress_thinking:
            return system
        prefix = self._NO_THINK_PREFIX
        return f"{prefix}\n{system}" if system else prefix

    @staticmethod
    def _extract_from_thinking(thinking: str) -> str:
        """Sólo se usa cuando suppress_thinking=False (uso explícito del razonamiento)."""
        thinking = thinking.strip()
        if not thinking:
            return ""
        match = re.search(r"\{.*\}", thinking, re.DOTALL)
        if match:
            return match.group(0)
        for marker in ("response:", "final answer:", "answer:", "result:"):
            lower = thinking.lower()
            if marker in lower:
                return thinking[lower.index(marker) + len(marker):].strip()
        return thinking


# ---------------------------------------------------------------------------
# Capa 1.5 – Transportes Avanzados (Fallback y Configurables)
# ---------------------------------------------------------------------------

class ConfigurableOllamaTransport(OllamaTransport):
    """
    Transporte que permite sobreescribir opciones por defecto (como num_predict)
    sin alterar las opciones que le pase el LLMClient en tiempo de ejecución.
    Esto es vital para dar más tokens a Qwen3 sin afectar a los modelos de fallback.
    """
    def __init__(
        self,
        model: str,
        default_options: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> None:
        super().__init__(model, **kwargs)
        self._default_options = default_options or {}

    def complete(self, prompt: str, system: Optional[str], options: Dict[str, Any], keep_alive: Any = None) -> str:
        # Las opciones pasadas por el LLMClient tienen prioridad,
        # pero fusionamos las por defecto para este modelo específico.
        merged_options = {**self._default_options, **options}
        return super().complete(prompt, system, merged_options, keep_alive=keep_alive)


class FallbackTransport(LLMTransport):
    """
    Transporte resiliente que encapsula múltiples modelos en cascada.

    Si el modelo primario devuelve una respuesta vacía (ej. Qwen3 agotó
    tokens en modo thinking), cambia instantáneamente al siguiente modelo
    de la lista sin consumir reintentos del LLMClient.
    """
    def __init__(self, transports: List[LLMTransport]) -> None:
        if not transports:
            raise ValueError("FallbackTransport requiere al menos un transporte")
        self.transports = transports

    def complete(self, prompt: str, system: Optional[str], options: Dict[str, Any], keep_alive: Any = None) -> str:
        last_error = None

        for i, transport in enumerate(self.transports):
            model_name = getattr(transport, 'model', f'model_{i}')
            try:
                raw = transport.complete(prompt, system, options, keep_alive=keep_alive)

                # Éxito: tiene contenido real
                if raw and raw.strip():
                    if i > 0:
                        writeLog("info", logger,
                                 f"[FallbackTransport] ✅ Success with fallback model '{model_name}'")
                    return raw

                # Fallo silencioso: el modelo devolvió vacío (caso típico de Qwen3)
                writeLog("warning", logger,
                         f"[FallbackTransport] Model '{model_name}' returned empty. Switching to next...")

            except Exception as exc:
                last_error = exc
                writeLog("warning", logger,
                         f"[FallbackTransport] Error with model '{model_name}': {exc}. Switching...")

        # Si agotamos todos los modelos, lanzamos el último error registrado
        err_msg = "All models in FallbackTransport failed."
        if last_error:
            err_msg += f" Last error: {last_error}"
        raise RuntimeError(err_msg)


# ---------------------------------------------------------------------------
# Capa 2 – Política de reintentos
# ---------------------------------------------------------------------------

@dataclass
class RetryPolicy:
    max_retries: int = 2

    def attempts(self) -> range:
        return range(self.max_retries + 1)

    def is_last(self, attempt: int) -> bool:
        return attempt >= self.max_retries


# ---------------------------------------------------------------------------
# Capa 3 – Constructor de prompts
# ---------------------------------------------------------------------------

class PromptBuilder:
    @staticmethod
    def enforce_json(prompt: str) -> str:
        return (
            f"{prompt}\n\n"
            "IMPORTANT: Respond ONLY with valid JSON (object or array). "
            "No explanations, no markdown fences."
        )


# ---------------------------------------------------------------------------
# Capa 4 – Parser de respuestas
# ---------------------------------------------------------------------------

class ResponseParser:
    """
    Limpia y extrae contenido útil de la respuesta cruda del LLM.

    Gestiona la Forma C del thinking: prosa de razonamiento en inglés
    emitida sin etiquetas 💭, antes del contenido real de la respuesta.
    """

    _CODE_FENCE  = re.compile(r"```(?:json)?\s*|\s*```")
    _THINK_BLOCK = re.compile(r"💭.*?🤔\s*", re.DOTALL | re.IGNORECASE)

    _UNTAGGED_THINKING_OPENERS = re.compile(
        r"^(Okay|Alright|Let me|I need to|I'll|Let's|So,|First,|The user|"
        r"Now,|Looking at|To answer|I should|We need|This query|The question)",
        re.IGNORECASE,
    )

    _REASONING_END_MARKERS = (
        "\n\n\n",
        "\n---\n",
        "\nSummary:", "\nResumen:", "\nRespuesta:", "\nResponse:",
    )

    _REASONING_PREFIXES = ("response:", "final answer:", "resumen:", "summary:")

    def clean_text(self, raw: str) -> str:
        text = self._THINK_BLOCK.sub("", raw).strip()
        text = self._CODE_FENCE.sub("", text).strip()
        text = self._strip_untagged_thinking(text)
        lower = text.lower()
        for prefix in self._REASONING_PREFIXES:
            if lower.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        return text

    def extract_json(self, text: str) -> Optional[Union[Dict, List]]:
        if not text:
            return None
        cleaned = self.clean_text(text)
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            result = self._balanced_extract(cleaned, open_ch, close_ch)
            if result is not None:
                return result
        match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return None

    def _strip_untagged_thinking(self, text: str) -> str:
        if not self._UNTAGGED_THINKING_OPENERS.match(text.strip()):
            return text

        for marker in self._REASONING_END_MARKERS:
            idx = text.find(marker)
            if idx != -1:
                candidate = text[idx + len(marker):].strip()
                if candidate:
                    writeLog("debug", logger,
                             f"[ResponseParser] Stripped untagged thinking "
                             f"({idx} chars) before marker {repr(marker)}")
                    return candidate

        paragraphs = text.split("\n\n")
        for i, para in enumerate(paragraphs):
            para = para.strip()
            if para and not self._UNTAGGED_THINKING_OPENERS.match(para):
                candidate = "\n\n".join(paragraphs[i:]).strip()
                if candidate:
                    writeLog("debug", logger,
                             f"[ResponseParser] Stripped {i} thinking paragraphs")
                    return candidate

        writeLog("warning", logger,
                 "[ResponseParser] Untagged thinking detected but no clear "
                 "boundary found; returning raw text")
        return text

    @staticmethod
    def _balanced_extract(text: str, open_ch: str, close_ch: str) -> Optional[Union[Dict, List]]:
        start = text.find(open_ch)
        if start == -1:
            return None
        depth = 0
        in_string = False
        escaped = False
        for i, ch in enumerate(text[start:], start=start):
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"' and not escaped:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    candidate = text[start: i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        try:
                            fixed = re.sub(r"'([^']*)'", r'"\1"', candidate)
                            return json.loads(fixed)
                        except json.JSONDecodeError:
                            return None
        return None


# ---------------------------------------------------------------------------
# Capa 5 – Fachada principal (LLMClient)
# ---------------------------------------------------------------------------

@dataclass
class LLMClientConfig:
    temperature: float = 0.0
    max_tokens: int = 1200
    top_p: float = 0.9
    repeat_penalty: float = 1.1


class LLMClient:
    """
    Fachada SOA para generación de texto y JSON con LLMs.
    Recibe sus dependencias por inyección (DIP).
    """

    def __init__(
        self,
        transport: LLMTransport,
        config: Optional[LLMClientConfig] = None,
        retry_policy: Optional[RetryPolicy] = None,
        parser: Optional[ResponseParser] = None,
    ) -> None:
        self._transport = transport
        self._config = config or LLMClientConfig()
        self._retry = retry_policy or RetryPolicy()
        self._parser = parser or ResponseParser()

    def generate_text(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        context: str = "",
        keep_alive: Any = None,
    ) -> str:
        options = self._build_options(temperature, max_tokens)
        for attempt in self._retry.attempts():
            try:
                raw = self._transport.complete(prompt, system_prompt, options, keep_alive=keep_alive)
                cleaned = self._parser.clean_text(raw)
                if cleaned:
                    return cleaned
                writeLog("warning", logger,
                         f"[LLMClient] Empty response (attempt {attempt + 1}) [{context}]")
            except Exception as exc:
                writeLog("exception", logger,
                         f"[LLMClient] Transport error (attempt {attempt + 1}) [{context}]: {exc}")
        writeLog("error", logger, f"[LLMClient] All attempts failed [{context}]")
        return ""

    def generate_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        context: str = "",
        expect_array: bool = False,
        keep_alive: Any = None,
    ) -> Union[Dict, List]:
        options = self._build_options(temperature, max_tokens)
        current_prompt = prompt
        empty: Union[Dict, List] = [] if expect_array else {}

        for attempt in self._retry.attempts():
            raw = self._transport.complete(current_prompt, system_prompt, options, keep_alive=keep_alive)
            if not raw:
                writeLog("warning", logger,
                         f"[LLMClient] Empty transport response (attempt {attempt + 1}) [{context}]")
            else:
                parsed = self._parser.extract_json(raw)
                if parsed is not None:
                    if self._type_matches(parsed, expect_array):
                        return parsed
                    writeLog("warning", logger,
                             f"[LLMClient] Type mismatch: expected "
                             f"{'list' if expect_array else 'dict'}, "
                             f"got {type(parsed).__name__} "
                             f"(attempt {attempt + 1}) [{context}]")
            if not self._retry.is_last(attempt):
                current_prompt = PromptBuilder.enforce_json(current_prompt)

        writeLog("error", logger,
                 f"[LLMClient] JSON extraction failed after all attempts [{context}]")
        return empty

    def generate_json_array(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        context: str = "",
        keep_alive: Any = None,
    ) -> List[str]:
        result = self.generate_json(
            prompt, system_prompt, temperature, max_tokens, context, expect_array=True,
            keep_alive=keep_alive,
        )
        if isinstance(result, list):
            return [str(item) for item in result]
        return []

    def generate_structured(
        self,
        prompt: str,
        model_class: Type[T],
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        context: str = "",
        keep_alive: Any = None,
    ) -> Optional[T]:
        json_data = self.generate_json(
            prompt, system_prompt, temperature, max_tokens, context, expect_array=False,
            keep_alive=keep_alive,
        )
        if not json_data:
            writeLog("error", logger,
                     f"[LLMClient] No JSON data for structured output [{context}]")
            return None
        try:
            return model_class(**json_data)
        except (ValidationError, TypeError) as exc:
            writeLog("error", logger,
                     f"[LLMClient] Validation error [{context}]: {exc}")
            return None

    def _build_options(
        self, temperature: Optional[float], max_tokens: Optional[int]
    ) -> Dict[str, Any]:
        return {
            "temperature": temperature if temperature is not None else self._config.temperature,
            "num_predict": max_tokens if max_tokens is not None else self._config.max_tokens,
            "top_p": self._config.top_p,
            "repeat_penalty": self._config.repeat_penalty,
        }

    @staticmethod
    def _type_matches(value: Any, expect_array: bool) -> bool:
        return isinstance(value, list) if expect_array else isinstance(value, dict)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_ollama_client(
    model: Optional[str] = None,
    max_retries: Optional[int] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    suppress_thinking: bool = True,
) -> LLMClient:
    """
    Factory estándar para obtener un LLMClient configurado sobre Ollama.
    Si no se pasan argumentos, lee la configuración de processControl.defaults.llm
    """
    defaults = _get_llm_defaults()

    m_model = model or defaults.get("primary_model", "llama3.2:3b")
    retries = max_retries if max_retries is not None else defaults.get("max_retries", 2)
    temp = temperature if temperature is not None else defaults.get("temperature", 0.0)
    tokens = max_tokens if max_tokens is not None else defaults.get("max_tokens", 1200)

    return LLMClient(
        transport=OllamaTransport(model=m_model, suppress_thinking=suppress_thinking),
        config=LLMClientConfig(temperature=temp, max_tokens=tokens),
        retry_policy=RetryPolicy(max_retries=retries),
    )


def create_resilient_ollama_client(
    primary_model: Optional[str] = None,
    fallback_models: Optional[List[str]] = None,
    max_retries: Optional[int] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    suppress_thinking: bool = True,
) -> LLMClient:
    """
    Factory recomendada para pipelines automáticos.

    Si no se pasan argumentos, lee TODA la configuración de
    processControl.defaults.llm (primario, fallbacks, tokens, etc.).

    Configura una cadena de rescate automática:
    1. Intenta con el modelo primario (ej. qwen3:8b) con tokens ampliados.
    2. Si devuelve vacío, intenta con el primer fallback (ej. qwen2.5:7b).
    3. Si falla, intenta con el segundo fallback (ej. llama3.1:8b).
    """
    defaults = _get_llm_defaults()

    p_model = primary_model or defaults.get("primary_model", "qwen3:8b")
    fb_models = fallback_models or defaults.get("fallback_model", ["qwen2.5:7b", "llama3.1:8b"])
    retries = max_retries if max_retries is not None else defaults.get("max_retries", 1)
    temp = temperature if temperature is not None else defaults.get("temperature", 0.3)
    tokens = max_tokens if max_tokens is not None else defaults.get("max_tokens", 800)

    transports: List[LLMTransport] = []

    # 1. Configurar el primario (ej. Qwen3)
    primary_options = {}
    if "qwen3" in p_model.lower():
        # Le damos 2.5x más espacio para que piense y aún le queden tokens para responder
        primary_options["num_predict"] = max(2000, int(tokens * 2.5))
        writeLog("info", logger,
                 f"[Factory] Qwen3 detected: bumping num_predict to {primary_options['num_predict']}")

    transports.append(
        ConfigurableOllamaTransport(
            model=p_model,
            default_options=primary_options,
            suppress_thinking=suppress_thinking
        )
    )

    # 2. Añadir los modelos de fallback (usan los tokens estándar)
    for fb_model in fb_models:
        transports.append(
            OllamaTransport(model=fb_model, suppress_thinking=suppress_thinking)
        )

    # 3. Crear el cliente usando el FallbackTransport
    fallback_transport = FallbackTransport(transports=transports)

    return LLMClient(
        transport=fallback_transport,
        config=LLMClientConfig(temperature=temp, max_tokens=tokens),
        retry_policy=RetryPolicy(max_retries=retries),
    )