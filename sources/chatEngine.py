#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chatEngine.py
=============

Motor de chat con RAG (Retrieval-Augmented Generation) sobre los resultados
del pipeline SLIP. Permite al investigador hacer preguntas en lenguaje natural
sobre los papers, conceptos y evidencias extraídas.

Arquitectura:
    1. Indexación: Carga papers_text.json, concept_evidence.json,
       concept_findings.json, papers_structured.json y technical_annex.json.
       Genera chunks de texto y sus embeddings con BAAI/bge-base-en-v1.5.
    2. Recuperación: Para una pregunta dada, busca los chunks más relevantes
       por similitud coseno.
    3. Generación: Usa Ollama (qwen3:8b) para responder basándose en los
       chunks recuperados, citando siempre el paper y sección de origen.

Uso:
    # Como módulo del pipeline:
    from sources.chatEngine import ChatEngine
    engine = ChatEngine(output_dir=_get_output_dir("SensoresACL"))
    respuesta = engine.preguntar("¿Qué métricas usa el paper ieee_25?")

    # Como aplicación web (Streamlit):
    streamlit run sources/chatEngine.py -- --ui
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sentence_transformers import SentenceTransformer

# Al ejecutarse directamente (p. ej. `streamlit run sources/chatEngine.py`)
# Python solo añade el directorio del script (`sources/`) a sys.path, no la
# raíz del proyecto. Se añade la raíz para poder importar `sources.*`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sources.common.common import logger, processControl, writeLog
from sources.common.translationService import TranslationService
from sources.common.utils import inicioModulo

# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
OLLAMA_MODEL = "qwen3:8b"
OLLAMA_BASE_URL = "http://localhost:11434"
SIMILARITY_THRESHOLD = 0.35
TOP_K_CHUNKS = 6
MAX_CHUNK_CHARS = 1200


def _get_output_dir(subject: str) -> Path:
    """Resuelve el directorio de output del subject respetando results_root.

    Usa processControl.env si está disponible (pipeline) o variables de
    entorno / valor por defecto (ejecución independiente de Streamlit).
    """
    env = getattr(processControl, "env", None) or {}
    results_root = env.get("results_root") or "results"
    output_rel = env.get("output", os.path.join(results_root, "output"))
    if os.path.isabs(output_rel):
        return Path(output_rel) / subject
    real_path = env.get("realPath", "")
    return Path(real_path) / output_rel / subject

# ---------------------------------------------------------------------------
# TRADUCCIÓN DE CONSULTAS (ES → EN para búsqueda semántica)
# ---------------------------------------------------------------------------
# El corpus y el modelo de embeddings están en inglés. Se traducen las
# consultas en español antes de generar el embedding de búsqueda para
# mejorar la similitud coseno. La pregunta original se mantiene para el LLM.

_ES_PATTERN = re.compile(
    r"\b(que|para|los|las|del|una|pero|también|sobre|entre|está|están|"
    r"más|como|con|por|sus|este|esta|estos|estas|desde|hasta|según|"
    r"mediante|dicha|dicho|así|ser|son|fue|han|siendo|cuando|donde|"
    r"qué|cuál|cuántos|cuántas|dónde|cuándo|cuál es|cuáles son)\b",
    re.IGNORECASE,
)
_ES_DIACRITICS = re.compile(r"[áéíóúñ¿¡ü]", re.IGNORECASE)


def _looks_spanish(text: str) -> bool:
    """Detecta si un texto está en español usando diacríticos y palabras clave."""
    if not text:
        return False
    if _ES_DIACRITICS.search(text):
        return True
    if _ES_PATTERN.search(text):
        return True
    return False


def _traducir_a_ingles(texto: str) -> str:
    """Traduce un texto del español al inglés para búsqueda semántica.

    Si el texto no parece español, o la traducción falla, se devuelve el
    original sin cambios para no romper el flujo.
    """
    if not _looks_spanish(texto):
        return texto
    try:
        from deep_translator import GoogleTranslator

        traducido = GoogleTranslator(source="es", target="en").translate(texto)
        if traducido and traducido.strip():
            writeLog(
                "info",
                logger,
                f"[ChatEngine] Consulta traducida ES→EN: {texto[:60]}... → "
                f"{traducido[:60]}...",
            )
            return traducido
    except Exception as exc:
        writeLog(
            "warning",
            logger,
            f"[ChatEngine] Traducción ES→EN falló: {exc}; usando texto original.",
        )
    return texto


# ---------------------------------------------------------------------------
# CLASE PRINCIPAL
# ---------------------------------------------------------------------------

class ChatEngine:
    """
    Motor de chat RAG sobre los resultados del pipeline SLIP.

    Carga los JSONs generados por el pipeline, indexa chunks de texto con
    embeddings y permite hacer preguntas en lenguaje natural.
    """

    def __init__(self, output_dir: Path | str):
        self.output_dir = Path(output_dir)
        self._embedder: Optional[SentenceTransformer] = None
        self._chunks: List[Dict[str, Any]] = []
        self._embeddings: Optional[np.ndarray] = None
        self._indexado = False

        # Cargar datos existentes si existen
        self._cargar_datos_existentes()

    # ------------------------------------------------------------------
    # CARGA DE DATOS
    # ------------------------------------------------------------------

    def _cargar_datos_existentes(self) -> None:
        """Intenta cargar papers_text.json, papers_metadata.json y concept_evidence.json si existen."""
        # Cargar metadatos para obtener títulos reales (papers_text.json no los tiene).
        metadata_titles: Dict[str, str] = {}
        metadata_file = self.output_dir / "papers_metadata.json"
        if metadata_file.exists():
            try:
                with open(metadata_file, "r", encoding="utf-8") as f:
                    papers_metadata = json.load(f)
                if isinstance(papers_metadata, list):
                    for m in papers_metadata:
                        pid = m.get("paper_id")
                        title = m.get("title") or ""
                        if pid:
                            metadata_titles[str(pid)] = title
                writeLog("info", logger,
                         f"[ChatEngine] Cargados {len(metadata_titles)} títulos de papers_metadata.json")
            except Exception as exc:
                writeLog("warning", logger,
                         f"[ChatEngine] No se pudo cargar papers_metadata.json: {exc}")

        papers_text_file = self.output_dir / "papers_text.json"
        if papers_text_file.exists():
            try:
                with open(papers_text_file, "r", encoding="utf-8") as f:
                    papers_text = json.load(f)
                self._indexar_papers_text(papers_text, metadata_titles)
                writeLog("info", logger,
                         f"[ChatEngine] Cargados {len(papers_text)} papers de papers_text.json")
            except Exception as exc:
                writeLog("warning", logger,
                         f"[ChatEngine] No se pudo cargar papers_text.json: {exc}")

        concept_evidence_file = self.output_dir / "concept_evidence.json"
        if concept_evidence_file.exists():
            try:
                with open(concept_evidence_file, "r", encoding="utf-8") as f:
                    concept_evidence = json.load(f)
                self._indexar_concept_evidence(concept_evidence)
                writeLog("info", logger,
                         f"[ChatEngine] Cargadas evidencias de {concept_evidence_file}")
            except Exception as exc:
                writeLog("warning", logger,
                         f"[ChatEngine] No se pudo cargar concept_evidence.json: {exc}")

        concept_findings_file = self.output_dir / "concept_findings.json"
        if concept_findings_file.exists():
            try:
                with open(concept_findings_file, "r", encoding="utf-8") as f:
                    concept_findings = json.load(f)
                self._indexar_concept_findings(concept_findings)
                writeLog("info", logger,
                         f"[ChatEngine] Cargados hallazgos de {concept_findings_file}")
            except Exception as exc:
                writeLog("warning", logger,
                         f"[ChatEngine] No se pudo cargar concept_findings.json: {exc}")

    def _indexar_papers_text(
        self,
        papers_text: List[Dict[str, Any]],
        metadata_titles: Optional[Dict[str, str]] = None,
    ) -> None:
        """Indexa texto limpio y secciones de papers_text.json."""
        metadata_titles = metadata_titles or {}
        for paper in papers_text:
            paper_id = str(paper.get("paper_id", "unknown"))
            # El título real está en papers_metadata.json (papers_text.json no lo tiene).
            title = metadata_titles.get(paper_id, "") or paper.get("title", "")

            # clean_text completo (truncado si es muy largo)
            clean_text = paper.get("clean_text", "")
            if clean_text and len(clean_text) > 100:
                self._agregar_chunk(
                    texto=clean_text[:MAX_CHUNK_CHARS],
                    paper_id=paper_id,
                    titulo=title,
                    seccion="texto_completo",
                    tipo="texto_completo",
                )

            # Secciones individuales
            clean_sections = paper.get("clean_sections", {}) or {}
            for seccion_nombre, seccion_texto in clean_sections.items():
                if seccion_texto and len(seccion_texto) > 100:
                    self._agregar_chunk(
                        texto=seccion_texto[:MAX_CHUNK_CHARS],
                        paper_id=paper_id,
                        titulo=title,
                        seccion=seccion_nombre,
                        tipo="seccion",
                    )

    def _indexar_concept_evidence(self, concept_evidence: Dict[str, Any]) -> None:
        """Indexa evidencias por concepto de concept_evidence.json."""
        concepts = concept_evidence.get("concepts", [])
        for concepto in concepts:
            concept_id = str(concepto.get("concept_id", "unknown"))
            concept_query = concepto.get("concept_query", "")

            for ku in concepto.get("knowledge_units", []):
                texto = ku.get("evidence_text", "")
                if texto and len(texto) > 50:
                    self._agregar_chunk(
                        texto=texto[:MAX_CHUNK_CHARS],
                        paper_id=ku.get("doc_id", "unknown"),
                        titulo=ku.get("paper_title", ""),
                        seccion=ku.get("section", ""),
                        zona=ku.get("zone", ""),
                        tipo="evidencia",
                        concepto_id=concept_id,
                        concepto_query=concept_query,
                    )

    def _indexar_concept_findings(self, concept_findings: Dict[str, Any]) -> None:
        """Indexa hallazgos sintetizados de concept_findings.json."""
        conceptos = concept_findings.get("conceptos", [])
        for concepto in conceptos:
            concept_id = str(concepto.get("concept_id", "unknown"))
            titulo = concepto.get("titulo", "")
            descripcion = concepto.get("descripcion", "")
            if descripcion:
                self._agregar_chunk(
                    texto=descripcion[:MAX_CHUNK_CHARS],
                    paper_id=f"concepto_{concept_id}",
                    titulo=titulo,
                    seccion="hallazgo_sintetizado",
                    tipo="hallazgo",
                    concepto_id=concept_id,
                )

    def _agregar_chunk(
        self,
        texto: str,
        paper_id: str,
        titulo: str,
        seccion: str,
        tipo: str = "texto",
        zona: str = "",
        concepto_id: Optional[str] = None,
        concepto_query: Optional[str] = None,
    ) -> None:
        """Agrega un chunk de texto al índice."""
        chunk = {
            "texto": texto,
            "paper_id": paper_id,
            "titulo": titulo,
            "seccion": seccion,
            "tipo": tipo,
            "zona": zona,
            "concepto_id": concepto_id,
            "concepto_query": concepto_query,
            "hash": hashlib.sha256(
                f"{paper_id}|{seccion}|{texto[:100]}".encode("utf-8")
            ).hexdigest()[:12],
        }
        self._chunks.append(chunk)

    # ------------------------------------------------------------------
    # INDEXACIÓN DE EMBEDDINGS
    # ------------------------------------------------------------------

    def _get_embedder(self) -> SentenceTransformer:
        """Carga el modelo de embeddings (perezoso)."""
        if self._embedder is None:
            writeLog("info", logger,
                     f"[ChatEngine] Cargando modelo de embeddings: {EMBEDDING_MODEL}")
            self._embedder = SentenceTransformer(EMBEDDING_MODEL)
        return self._embedder

    def indexar(self) -> None:
        """Genera embeddings para todos los chunks cargados."""
        if not self._chunks:
            writeLog("warning", logger,
                     "[ChatEngine] No hay chunks para indexar. "
                     "¿Existen papers_text.json o concept_evidence.json?")
            return

        writeLog("info", logger,
                 f"[ChatEngine] Generando embeddings para {len(self._chunks)} chunks...")

        embedder = self._get_embedder()
        textos = [c["texto"] for c in self._chunks]
        embeddings = embedder.encode(
            textos,
            normalize_embeddings=True,
            batch_size=32,
            show_progress_bar=True,
        )

        self._embeddings = embeddings
        self._indexado = True
        writeLog("info", logger,
                 f"[ChatEngine] Indexación completa: {len(self._chunks)} chunks indexados")

    # ------------------------------------------------------------------
    # RECUPERACIÓN
    # ------------------------------------------------------------------

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """Calcula similitud coseno entre un vector y una matriz."""
        return a @ b.T  # Ya están normalizados

    def buscar(self, pregunta: str, top_k: int = TOP_K_CHUNKS) -> List[Dict[str, Any]]:
        """
        Busca los chunks más relevantes para una pregunta.

        Returns:
            Lista de diccionarios con los chunks más relevantes y su similitud.
        """
        if not self._indexado:
            self.indexar()

        if not self._chunks:
            return []

        # Traducir la consulta a inglés para búsqueda semántica (corpus + modelo en inglés).
        # La pregunta original se conserva para el prompt del LLM (responde en español).
        pregunta_para_buscar = _traducir_a_ingles(pregunta)

        embedder = self._get_embedder()
        query_emb = embedder.encode([pregunta_para_buscar], normalize_embeddings=True)[0]

        similitudes = self._cosine_similarity(query_emb, self._embeddings)

        # Obtener índices de los top_k más similares
        top_indices = np.argsort(similitudes)[::-1][:top_k]

        resultados = []
        for idx in top_indices:
            similitud = float(similitudes[idx])
            if similitud < SIMILARITY_THRESHOLD:
                continue
            chunk = dict(self._chunks[idx])
            chunk["similitud"] = round(similitud, 4)
            resultados.append(chunk)

        return resultados

    def buscar_exhaustiva(self, pregunta: str) -> Dict[str, Any]:
        """
        Búsqueda exhaustiva en TODOS los chunks del corpus (batch).

        Recorre cada chunk, calcula similitud con la pregunta y devuelve
        todos los que superen el umbral, agrupados por paper.

        Returns:
            Diccionario con:
              - 'total_chunks_evaluados': número de chunks revisados
              - 'papers_coincidentes': lista de dicts con paper_id, titulo,
                mejores_chunks (lista de chunks relevantes de ese paper),
                mejor_similitud (máxima similitud de ese paper).
        """
        if not self._indexado:
            self.indexar()

        if not self._chunks:
            return {
                "total_chunks_evaluados": 0,
                "papers_coincidentes": [],
            }

        # Traducir consulta a inglés para búsqueda semántica.
        pregunta_para_buscar = _traducir_a_ingles(pregunta)

        embedder = self._get_embedder()
        query_emb = embedder.encode([pregunta_para_buscar], normalize_embeddings=True)[0]

        similitudes = self._cosine_similarity(query_emb, self._embeddings)

        # Ordenar todos los índices por similitud descendente.
        orden = np.argsort(similitudes)[::-1]

        # Agrupar chunks relevantes por paper.
        papers: Dict[str, Dict[str, Any]] = {}
        for idx in orden:
            similitud = float(similitudes[idx])
            if similitud < SIMILARITY_THRESHOLD:
                continue
            chunk = dict(self._chunks[idx])
            chunk["similitud"] = round(similitud, 4)
            pid = chunk["paper_id"]
            if pid not in papers:
                papers[pid] = {
                    "paper_id": pid,
                    "titulo": chunk.get("titulo", ""),
                    "mejores_chunks": [],
                    "mejor_similitud": similitud,
                }
            papers[pid]["mejores_chunks"].append(chunk)
            # Mantener la mejor similitud del paper (la primera es la máxima por orden).
            if similitud > papers[pid]["mejor_similitud"]:
                papers[pid]["mejor_similitud"] = similitud

        # Ordenar papers por su mejor similitud descendente.
        papers_lista = sorted(
            papers.values(),
            key=lambda p: p["mejor_similitud"],
            reverse=True,
        )

        return {
            "total_chunks_evaluados": len(self._chunks),
            "papers_coincidentes": papers_lista,
        }

    # ------------------------------------------------------------------
    # GENERACIÓN (LLM vía Ollama)
    # ------------------------------------------------------------------

    def _construir_prompt(self, pregunta: str, chunks: List[Dict[str, Any]]) -> str:
        """Construye el prompt para el LLM con los chunks como contexto."""
        contexto_partes = []
        for i, chunk in enumerate(chunks, 1):
            cita = f"[{i}] {chunk['paper_id']} (sección: {chunk['seccion']})"
            contexto_partes.append(f"[{i}] {chunk['texto']}\n   Fuente: {cita}")

        contexto = "\n\n".join(contexto_partes)

        prompt = f"""Eres un asistente experto en análisis de literatura científica.
Tu tarea es responder preguntas basándote ÚNICAMENTE en el contexto proporcionado
extraído de papers académicos.

INSTRUCCIONES:
- Responde en español.
- Usa ÚNICAMENTE la información proporcionada en el contexto.
- Si la información no está en el contexto, di que no lo sabes.
- Cita siempre las fuentes usando el formato [número] que corresponde al contexto.
- Sé preciso y conciso.
- No inventes información.

CONTEXTO:
{contexto}

PREGUNTA: {pregunta}

RESPUESTA:"""

        return prompt

    def _llm_generar(self, prompt: str) -> str:
        """Envía el prompt a Ollama y devuelve la respuesta."""
        import requests

        try:
            respuesta = requests.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "temperature": 0.3,
                    "max_tokens": 1000,
                    "stream": False,
                },
                timeout=120,
            )
            respuesta.raise_for_status()
            datos = respuesta.json()
            return datos.get("response", "No se obtuvo respuesta del LLM.")
        except Exception as exc:
            writeLog("error", logger, f"[ChatEngine] Error llamando a Ollama: {exc}")
            return f"Error al contactar con Ollama: {exc}"

    def preguntar(self, pregunta: str, top_k: int = TOP_K_CHUNKS) -> Dict[str, Any]:
        """
        Responde una pregunta usando RAG.

        Returns:
            Diccionario con 'respuesta', 'fuentes' y 'chunks_usados'.
        """
        chunks = self.buscar(pregunta, top_k=top_k)

        if not chunks:
            return {
                "respuesta": (
                    "No encontré información relevante en los papers procesados. "
                    "¿Podrías reformular la pregunta o revisar que el pipeline "
                    "haya procesado papers correctamente?"
                ),
                "fuentes": [],
                "chunks_usados": [],
            }

        prompt = self._construir_prompt(pregunta, chunks)
        respuesta_llm = self._llm_generar(prompt)

        fuentes = []
        for chunk in chunks:
            fuentes.append({
                "paper_id": chunk["paper_id"],
                "titulo": chunk.get("titulo", ""),
                "seccion": chunk["seccion"],
                "tipo": chunk["tipo"],
                "similitud": chunk["similitud"],
                "texto_preview": chunk["texto"][:200] + "...",
                "texto_completo": chunk["texto"],
            })

        return {
            "respuesta": respuesta_llm,
            "fuentes": fuentes,
            "chunks_usados": len(chunks),
        }

    def obtener_estado(self) -> Dict[str, Any]:
        """Devuelve información sobre el estado del motor de chat."""
        return {
            "chunks_indexados": len(self._chunks),
            "indexado": self._indexado,
            "embedder_cargado": self._embedder is not None,
            "output_dir": str(self.output_dir),
        }


# ---------------------------------------------------------------------------
# INTERFAZ STREAMLIT
# ---------------------------------------------------------------------------

def _inicializar_session_state() -> None:
    """Inicializa el estado de la sesión de Streamlit."""
    import streamlit as st

    if "chat_engine" not in st.session_state:
        subject = getattr(processControl.args, "subject", "SensoresACL")
        output_dir = _get_output_dir(subject)
        st.session_state.chat_engine = ChatEngine(output_dir)
        st.session_state.chat_engine.indexar()
        st.session_state.historial = []

    if "translation_svc" not in st.session_state:
        subject = getattr(processControl.args, "subject", "SensoresACL")
        output_dir = _get_output_dir(subject)
        cache_file = output_dir / "translation_cache.json"
        st.session_state.translation_svc = TranslationService(
            cache_file=cache_file,
            backend="marian",
            model_name="Helsinki-NLP/opus-mt-en-es",
        )
        st.session_state.translation_svc.load_cache()


def _render_interfaz() -> None:
    """Renderiza la interfaz de Streamlit."""
    import streamlit as st

    st.set_page_config(
        page_title="SLIP - Chat Científico",
        page_icon="📚",
        layout="wide",
    )

    st.title("📚 SLIP - Chat Científico")
    st.caption("Haz preguntas sobre los papers y hallazgos procesados por SLIP")

    # Inicializar
    if "chat_engine" not in st.session_state:
        _inicializar_session_state()

    engine: ChatEngine = st.session_state.chat_engine

    # Mostrar estado
    with st.sidebar:
        st.header("⚙️ Estado")
        estado = engine.obtener_estado()
        st.metric("Chunks indexados", estado["chunks_indexados"])
        st.metric("Indexado", "✅" if estado["indexado"] else "❌")
        st.metric("Embeddings", "✅" if estado["embedder_cargado"] else "❌")

        st.divider()
        st.header("🔍 Modo de búsqueda")
        busqueda_exhaustiva = st.toggle(
            "Búsqueda exhaustiva en todo el corpus",
            value=st.session_state.get("busqueda_exhaustiva", False),
            help="Si está activado, revisa TODOS los chunks del corpus "
                 "(más lento, pero cubre preguntas de agregación/conteo).",
        )
        st.session_state.busqueda_exhaustiva = busqueda_exhaustiva

        st.divider()
        st.header("📊 Estadísticas")
        if st.session_state.historial:
            st.metric("Preguntas esta sesión", len(st.session_state.historial))

    # Chat principal
    st.header("💬 Haz tu pregunta")

    # Mostrar historial
    for entrada in st.session_state.historial:
        with st.chat_message("usuario"):
            st.write(entrada["pregunta"])
        with st.chat_message("asistente"):
            st.write(entrada["respuesta"])
            with st.expander("Ver fuentes"):
                for fuente in entrada["fuentes"]:
                    titulo_hist = fuente.get("titulo") or "Sin título"
                    st.write(
                        f"[{fuente['paper_id']}] {titulo_hist} "
                        f"(sección: {fuente['seccion']}, similitud: {fuente['similitud']})"
                    )

    # Input nuevo
    pregunta = st.chat_input("Escribe tu pregunta aquí...")

    if pregunta:
        # Mostrar pregunta del usuario
        with st.chat_message("usuario"):
            st.write(pregunta)

        # Generar respuesta
        with st.chat_message("asistente"):
            if st.session_state.get("busqueda_exhaustiva", False):
                # --- MODO EXHAUSTIVO (batch) ---
                with st.spinner("Buscando en TODO el corpus..."):
                    resultado = engine.buscar_exhaustiva(pregunta)

                st.write(
                    f"Se evaluaron **{resultado['total_chunks_evaluados']} chunks** "
                    f"del corpus. Se encontraron coincidencias en "
                    f"**{len(resultado['papers_coincidentes'])} papers**."
                )

                if not resultado["papers_coincidentes"]:
                    st.info("No se encontraron papers relevantes para esta consulta.")
                else:
                    for paper in resultado["papers_coincidentes"]:
                        titulo_paper = paper.get("titulo") or "Sin título"
                        with st.expander(
                            f"📄 {paper['paper_id']} — {titulo_paper}",
                            expanded=False,
                        ):
                            st.write(
                                f"**Mejor similitud:** {paper['mejor_similitud']:.4f}"
                            )
                            for i, chunk in enumerate(paper["mejores_chunks"], 1):
                                st.markdown(
                                    f"**Fragmento {i}** (sección: {chunk['seccion']}, "
                                    f"similitud: {chunk['similitud']:.4f})"
                                )
                                texto_fragmento = st.session_state.translation_svc.translate(chunk["texto"])
                                st.write(texto_fragmento)

            else:
                # --- MODO RÁPIDO (RAG top-6 con LLM) ---
                with st.spinner("Buscando y generando respuesta..."):
                    resultado = engine.preguntar(pregunta)

                st.write(resultado["respuesta"])

                # Mostrar fuentes con expander para ver extracto completo
                if resultado["fuentes"]:
                    with st.expander(f"📚 Fuentes ({len(resultado['fuentes'])})"):
                        for fuente in resultado["fuentes"]:
                            titulo_fuente = fuente.get("titulo") or "Sin título"
                            with st.expander(
                                f"• {fuente['paper_id']} — {titulo_fuente}",
                                expanded=False,
                            ):
                                st.write(
                                    f"**Sección:** {fuente['seccion']}  |  "
                                    f"**Tipo:** {fuente['tipo']}  |  "
                                    f"**Similitud:** {fuente['similitud']:.4f}"
                                )
                                st.write("**Extracto:**")
                                texto_completo = fuente["texto_completo"]
                                texto_traducido = st.session_state.translation_svc.translate(texto_completo)
                                st.write(texto_traducido)

        # Guardar en historial
        fuentes_historial = (
            resultado.get("fuentes", [])
            if isinstance(resultado, dict)
            else []
        )
        respuesta_historial = (
            resultado.get("respuesta", "")
            if isinstance(resultado, dict)
            else ""
        )
        st.session_state.historial.append({
            "pregunta": pregunta,
            "respuesta": respuesta_historial,
            "fuentes": fuentes_historial,
        })

        # Persistir caché de traducción
        st.session_state.translation_svc.save_cache()


def main() -> None:
    """Punto de entrada para ejecutar como aplicación Streamlit."""
    import sys

    # Verificar si se pide modo UI
    if "--ui" in sys.argv or "streamlit" in sys.argv[0]:
        _render_interfaz()
    else:
        # Modo línea de comandos (para pruebas)
        import sys as sys_mod

        if len(sys_mod.argv) < 2:
            print("Uso: python sources/chatEngine.py \"tu pregunta\"")
            print("       o: streamlit run sources/chatEngine.py")
            return

        pregunta = " ".join(sys_mod.argv[1:])
        subject = getattr(processControl.args, "subject", "SensoresACL")
        output_dir = _get_output_dir(subject)

        engine = ChatEngine(output_dir)
        engine.indexar()

        resultado = engine.preguntar(pregunta)
        print("\n" + "=" * 60)
        print("RESPUESTA:")
        print("=" * 60)
        print(resultado["respuesta"])
        print("\n" + "=" * 60)
        print("FUENTES:")
        print("=" * 60)
        for fuente in resultado["fuentes"]:
            print(
                f"  [{fuente['paper_id']}] {fuente['titulo']} "
                f"(sección: {fuente['seccion']}, sim: {fuente['similitud']})"
            )
        print("=" * 60)


if __name__ == "__main__":
    main()