"""
processExistsOverviewCheck.py
==============================
Módulo de auditoría local para verificar la coherencia y exactitud de los
resúmenes generados para EXIST 2026 frente a los PDFs originales.

Lee 'resumenes.txt' y 'bibtex.txt' desde el directorio de entrada, localiza
los PDFs automáticamente extrayendo claves/títulos y utiliza Qwen (vía LLMClient)
para detectar alucinaciones o errores metodológicos.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, validator

# Importaciones del proyecto (arquitectura SOA)
from sources.common.common import logger, writeLog
from sources.common.llm_client import LLMClient, create_ollama_client
from sources.common.utils import inicioModulo

from sources.common.fullTextExtractionEngine import extract_pdf_text



# ==============================================================================
# Configuración
# ==============================================================================

LLM_MODEL = "qwen3:8b"         # Modelo local a usar
MAX_PDF_CHARS = 28000          # Límite de contexto para no saturar al LLM
PDF_DIR_NAME = "pdfs_exist2026" # Nombre de la subcarpeta donde están los PDFs


# ==============================================================================
# Objetos de Dominio (Pydantic)
# ==============================================================================

class VerificationResult(BaseModel):
    """Estructura de la auditoría devuelta por el LLM."""
    puntuacion: int = Field(..., description="Puntuación de 1 a 10 (10 = perfecto)")
    es_exacto: bool = Field(..., description="True si no hay alucinaciones graves")
    alucinaciones: List[str] = Field(default_factory=list, description="Frases inventadas")
    omisiones_graves: List[str] = Field(default_factory=list, description="Faltas cruciales")
    justificacion: str = Field(..., description="Explicación breve")

    @validator('puntuacion')
    def check_range(cls, v):
        if not 1 <= v <= 10:
            raise ValueError("La puntuación debe estar entre 1 y 10")
        return v


# ==============================================================================
# Capa 1 – Lógica de Mapeo (BibTeX -> PDF) [ACTUALIZADA]
# ==============================================================================

# ==============================================================================
# Capa 1 – Lógica de Mapeo (BibTeX -> PDF) [CORREGIDA PARA LATEX ANIDADO]
# ==============================================================================

def _extract_bibtex_field(block: str, field_name: str) -> str:
    """
    Extrae un campo de un bloque BibTeX manejando correctamente
    el anidamiento de llaves de LaTeX (ej. {LLM}, {OCR}).
    """
    match = re.search(rf'{field_name}\s*=\s*([{{"])', block, re.IGNORECASE)
    if not match:
        return ""

    start_idx = match.end() - 1
    delimiter = match.group(1)

    if delimiter == '"':
        # Si usa comillas, busca la siguiente comilla
        end_idx = block.find('"', start_idx + 1)
        if end_idx == -1: return ""
        return block[start_idx + 1: end_idx].strip()

    elif delimiter == '{':
        # Si usa llaves, hace balanceo de profundidad para ignorar llaves internas
        depth = 0
        in_string = False
        escaped = False

        for i in range(start_idx, len(block)):
            c = block[i]
            if escaped:
                escaped = False
                continue
            if c == '\\':
                escaped = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    # Encontró la llave de cierre real
                    return block[start_idx + 1: i].strip()

    return ""


def _extract_bibtex_entries(bib_text: str) -> List[Tuple[str, str]]:
    """Divide el archivo en bloques y extrae (clave, título_limpio) de cada uno."""
    entries = []
    # Dividir por líneas que empiezan con @ (inicio de un nuevo bloque)
    blocks = re.split(r'\n(?=@)', bib_text)

    for block in blocks:
        if not block.strip().startswith('@InProceedings'):
            continue  # Ignoramos @book (zz-CLEF2026WN) u otros

        # 1. Extraer clave (ej. "Wang2026")
        key_match = re.search(r'@\w+\{([^,]+)', block)
        if not key_match:
            continue
        key = key_match.group(1).strip()

        # 2. Extraer título usando el parser robusto
        title = _extract_bibtex_field(block, 'title')

        # 3. Limpiar caracteres LaTeX residuales para la búsqueda posterior
        clean_title = re.sub(r'[{}\\]', ' ', title)

        if clean_title.strip():
            entries.append((key, clean_title.strip()))

    return entries


def build_bibtex_mapping(bib_path: Path, pdf_dir: Path) -> Dict[str, Path]:
    """
    Parsea bibtex.txt y empareja cada clave/título con el PDF correspondiente.
    """
    if not bib_path.exists():
        logger.error(f"[ExistsCheck] No se encontró el archivo {bib_path}")
        return {}

    bib_text = bib_path.read_text(encoding='utf-8')

    # Extraer pares usando el parser robusto
    entries = _extract_bibtex_entries(bib_text)

    if not entries:
        logger.error("[ExistsCheck] No se pudieron extraer claves/títulos del bibtex.txt")
        return {}

    mapping = {}
    unmatched_keys = [e[0] for e in entries]

    if not pdf_dir.exists():
        logger.error(f"[ExistsCheck] El directorio de PDFs no existe: {pdf_dir}")
        return {}

    # Iterar sobre los PDFs y buscar coincidencias leyendo su contenido
    for pdf_file in pdf_dir.glob("*.pdf"):
        try:
            # Leemos solo el inicio del PDF (títulos suelen estar en la 1ª página)
            pdf_text_sample = extract_pdf_text(str(pdf_file))[:5000].lower()
        except Exception as e:
            writeLog("warning", logger, f"[ExistsCheck] Error leyendo {pdf_file.name}: {e}")
            continue

        if not pdf_text_sample.strip():
            continue

        for key, title in entries:
            if key in mapping:
                continue  # Ya fue mapeada a otro PDF

            key_lower = key.lower()

            # Estrategia 1: La clave bibtex exacta está en el texto del PDF
            if key_lower in pdf_text_sample:
                mapping[key] = pdf_file
                if key in unmatched_keys: unmatched_keys.remove(key)
                continue

            # Estrategia 2: Coincidencia por palabras clave del título
            # Extraemos palabras > 3 letras y las ordenamos por longitud (más específicas primero)
            words = [w.lower() for w in title.split() if len(w) > 3]
            words.sort(key=len, reverse=True)
            search_words = words[:4]  # Tomamos las 4 más distintivas

            if not search_words:
                continue

            # Si al menos 3 de las 4 palabras clave están en el PDF, es un match
            matches = sum(1 for w in search_words if w in pdf_text_sample)
            if matches >= 3:
                mapping[key] = pdf_file
                if key in unmatched_keys: unmatched_keys.remove(key)

    # Log de los que no se pudieron emparejar
    if unmatched_keys:
        writeLog("warning", logger,
                 f"[ExistsCheck] No se encontró PDF para las claves: {', '.join(unmatched_keys)}")

    return mapping


# ==============================================================================
# Capa 2 – Extracción de Párrafos
# ==============================================================================

def extract_paragraphs(summary_path: Path) -> List[Tuple[str, str, str]]:
    """Extrae párrafos individuales del resumen."""
    if not summary_path.exists():
        return []

    text = summary_path.read_text(encoding='utf-8')
    pattern = r'((?:[A-Z][A-Za-z0-9_\-]+(?:\s+[A-Z][A-Za-z0-9_\-]+)*)\s*(?:~?\\cite\{([^}]+)\}|\[(\d+)\]))\s*([^\n]+(?:\n(?![A-Z][A-Za-z0-9_\-]+(?:\s|~?\\cite|\[))[^\n]+)*)'

    paragraphs = []
    for match in re.finditer(pattern, text):
        team_name, cite_key, num_ref, paragraph_text = match.groups()
        key = cite_key if cite_key else num_ref
        clean_text = re.sub(r'\\cite\{[^}]+\}', '[REF]', paragraph_text).strip()
        paragraphs.append((team_name.strip(), key, clean_text))

    return paragraphs


# ==============================================================================
# Capa 3 – Verificador (Motor LLM)
# ==============================================================================

class SummaryVerifier:
    def __init__(self, llm_client: LLMClient, bibtex_mapping: Dict[str, Path]):
        self._llm = llm_client
        self._mapping = bibtex_mapping
        self._pdf_cache: Dict[str, str] = {}

    def _get_pdf_text(self, pdf_path: Path) -> str:
        """Carga el texto del PDF truncándolo para no exceder el contexto del LLM."""
        if str(pdf_path) not in self._pdf_cache:
            full_text = extract_pdf_text(str(pdf_path))
            self._pdf_cache[str(pdf_path)] = full_text[:MAX_PDF_CHARS]
        return self._pdf_cache[str(pdf_path)]

    def verify_paragraph(self, paragraph_text: str, pdf_path: Path, team_name: str) -> VerificationResult:
        """Envía el resumen y el PDF al LLM para auditoría estricta."""
        pdf_text = self._get_pdf_text(pdf_path)

        if not pdf_text.strip():
            return VerificationResult(
                puntuacion=0, es_exacto=False,
                alucinaciones=["PDF vacío o ilegible"],
                omisiones_graves=[],
                justificacion="No se pudo extraer texto del PDF"
            )

        system_prompt = """Eres un auditor académico estricto. Compara un RESUMEN contra el texto ORIGINAL de un PDF.
Evalúa si hay ALUCINACIONES (afirmaciones de modelos, técnicas, tareas o resultados que NO están en el PDF), 
ERRORES (ej. decir Task 1 cuando el PDF dice Task 2), o si es COHERENTE.
No penalices omitir hiperparámetros, pero SÍ penaliza inventar rankings, modelos o subtareas."""

        user_prompt = f"""TEXTO ORIGINAL DEL PDF (Equipo: {team_name}):
---
{pdf_text}
---

RESUMEN A EVALUAR:
---
{paragraph_text}
---

Responde SOLO con este JSON:
{{
  "puntuacion": <int 1-10>,
  "es_exacto": <bool true si puntuacion >= 7>,
  "alucinaciones": ["<frase falsa>" o []],
  "omisiones_graves": ["<falta crucial>" o []],
  "justificacion": "<Explicación de 1-2 frases>"
}}"""

        result = self._llm.generate_structured(
            prompt=user_prompt,
            model_class=VerificationResult,
            system_prompt=system_prompt,
            context=f"Auditoría de {team_name}",
            temperature=0.1
        )

        return result if result else VerificationResult(
            puntuacion=0, es_exacto=False,
            alucinaciones=["Error al procesar el LLM"],
            omisiones_graves=[],
            justificacion="Fallo en la llamada al LLM"
        )

    def run_audit(self, summary_path: Path, output_path: Optional[Path] = None) -> List[Dict]:
        """Ejecuta la verificación completa, imprime en consola y guarda JSON."""
        paragraphs = extract_paragraphs(summary_path)
        if not paragraphs:
            logger.error("[ExistsCheck] No se pudieron extraer párrafos de resumenes.txt.")
            return []

        print("\n" + "=" * 80)
        print(f" 🔍 INICIANDO AUDITORÍA DE RESÚMENES EXIST 2026 ({len(paragraphs)} equipos)")
        print("=" * 80 + "\n")

        total_score = 0
        hallucinations_count = 0
        report_data = []

        for i, (team, key, text) in enumerate(paragraphs, 1):
            # Obtener el path y extraer solo el nombre del archivo
            pdf_path = self._mapping.get(key)
            pdf_name = pdf_path.name if pdf_path else "NO ENCONTRADO"

            print(f"[{i}/{len(paragraphs)}] Analizando: {team} ({key}) -> {pdf_name}")

            if not pdf_path or not pdf_path.exists():
                print(f"    ❌ ERROR: PDF no encontrado en disco. Saltando.\n")
                continue

            result = self.verify_paragraph(text, pdf_path, team)
            total_score += result.puntuacion

            # Formateo visual de consola
            color = "\033[92m" if result.puntuacion >= 8 else ("\033[93m" if result.puntuacion >= 5 else "\033[91m")
            reset = "\033[0m"
            status_icon = "✅" if result.es_exacto else "⚠️"

            print(f"    {status_icon} Grado de acierto: {color}{result.puntuacion}/10{reset}")
            print(f"    📝 Justificación: {result.justificacion}")

            if result.alucinaciones:
                hallucinations_count += 1
                print(f"    🚨 ALUCINACIONES DETECTADAS:")
                for aluc in result.alucinaciones: print(f"       - {aluc}")

            if result.omisiones_graves:
                print(f"    📉 OMISIONES GRAVES:")
                for omis in result.omisiones_graves: print(f"       - {omis}")
            print("-" * 80 + "\n")

            report_data.append({
                "team": team, "key": key, "score": result.puntuacion,
                "is_accurate": result.es_exacto, "justification": result.justificacion,
                "hallucinations": result.alucinaciones, "grave_omissions": result.omisiones_graves
            })

        avg_score = total_score / len(paragraphs) if paragraphs else 0
        print("=" * 80)
        print(" 📊 RESUMEN GLOBAL DE LA AUDITORÍA")
        print("=" * 80)
        print(f" Puntuación Media: {avg_score:.1f} / 10")
        print(f" Resúmenes con alucinaciones: {hallucinations_count} / {len(paragraphs)}")
        print("=" * 80 + "\n")

        if output_path:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "average_score": avg_score,
                    "total_hallucinations": hallucinations_count,
                    "details": report_data
                }, f, indent=2, ensure_ascii=False)
            writeLog("info", logger, f"[ExistsCheck] Reporte guardado en {output_path}")

        return report_data


# ==============================================================================
# Punto de entrada oficial
# ==============================================================================

def processExistsOverviewCheck() -> Optional[List[Dict]]:
    """Punto de entrada del módulo compatible con main.py"""
    input_dir, output_dir = inicioModulo('processExistsOverviewCheck')

    summary_file = input_dir / "resumenes.txt"
    bibtex_file = input_dir / "bibtex.txt"
    pdf_dir = input_dir / PDF_DIR_NAME
    output_report = output_dir / "audit_report.json"

    if not summary_file.exists() or not bibtex_file.exists():
        logger.error(f"[ExistsCheck] Faltan archivos en {input_dir}. Necesito 'resumenes.txt' y 'bibtex.txt'.")
        return None

    writeLog("info", logger, f"[ExistsCheck] Construyendo mapa de PDFs (leyendo contenidos)...")
    bibtex_mapping = build_bibtex_mapping(bibtex_file, pdf_dir)
    writeLog("info", logger, f"[ExistsCheck] Mapeo exitoso: {len(bibtex_mapping)} equipos vinculados.")

    writeLog("info", logger, f"[ExistsCheck] Conectando con Ollama (modelo: {LLM_MODEL})...")
    client = create_ollama_client(
        model=LLM_MODEL, temperature=0.1, max_tokens=1500, suppress_thinking=True
    )

    verifier = SummaryVerifier(llm_client=client, bibtex_mapping=bibtex_mapping)
    return verifier.run_audit(summary_file, output_path=output_report)