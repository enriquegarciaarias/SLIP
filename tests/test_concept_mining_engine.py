"""
Tests unitarios para conceptMiningEngine.py

Cubre las funciones destacadas en SESSION_STATE.md como deuda técnica:
  - extract_structured_terms
  - build_enriched_text
  - deduplicate_semantic_terms (NUEVO: deduplicación semántica de keywords)

Además de otras funciones del módulo que pueden probarse sin LLM.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from sources.conceptMiningEngine import (
    deduplicate_semantic_terms,
    extract_structured_terms,
    build_enriched_text,
    load_structured_papers,
    load_zoned_papers,
)


class TestExtractStructuredTerms(unittest.TestCase):
    """Pruebas para extract_structured_terms."""

    def test_empty_structured_returns_empty(self):
        """Si structured está vacío o skipped, devuelve lista vacía."""
        self.assertEqual(extract_structured_terms({}), [])
        self.assertEqual(extract_structured_terms(None), [])
        self.assertEqual(extract_structured_terms({"skipped": True}), [])

    def test_extracts_contributions(self):
        """Debe extraer los términos de 'contributions'."""
        structured = {
            "contributions": ["proponemos un nuevo método", "mejoramos la precisión"],
        }
        terms = extract_structured_terms(structured)
        self.assertIn("proponemos un nuevo método", terms)
        self.assertIn("mejoramos la precisión", terms)

    def test_extracts_methodology_fields(self):
        """Debe extraer approach, architecture, datasets, baselines, metrics, signal_modalities."""
        structured = {
            "methodology": {
                "approach": "aprendizaje profundo",
                "architecture": "transformer",
                "datasets": ["PhysioNet", "MIMIC"],
                "baselines": ["baseline A", "baseline B"],
                "metrics": ["F1-score", "accuracy"],
                "signal_modalities": ["ECG", "PPG"],
            }
        }
        terms = extract_structured_terms(structured)
        self.assertIn("aprendizaje profundo", terms)
        self.assertIn("transformer", terms)
        self.assertIn("PhysioNet", terms)
        self.assertIn("MIMIC", terms)
        self.assertIn("baseline A", terms)
        self.assertIn("F1-score", terms)
        self.assertIn("ECG", terms)

    def test_extracts_results_fields(self):
        """Debe extraer main, ablation, key_numbers, tables_referenced."""
        structured = {
            "results": {
                "main": "mejor precisión global",
                "ablation": ["sin módulo A", "sin módulo B"],
                "key_numbers": ["85.5% accuracy", "F1=0.92"],
                "tables_referenced": ["Tabla 3", "Tabla 5"],
            }
        }
        terms = extract_structured_terms(structured)
        self.assertIn("mejor precisión global", terms)
        self.assertIn("sin módulo A", terms)
        self.assertIn("85.5% accuracy", terms)
        self.assertIn("Tabla 3", terms)

    def test_extracts_limitations_and_future_work(self):
        """Debe extraer hasta 3 limitaciones y 3 trabajos futuros."""
        structured = {
            "limitations": ["limitación 1", "limitación 2", "limitación 3", "limitación 4"],
            "future_work": ["futuro 1", "futuro 2", "futuro 3", "futuro 4"],
        }
        terms = extract_structured_terms(structured)
        self.assertIn("limitación 1", terms)
        self.assertIn("limitación 2", terms)
        self.assertIn("limitación 3", terms)
        self.assertNotIn("limitación 4", terms)  # solo los primeros 3
        self.assertIn("futuro 1", terms)
        self.assertIn("futuro 2", terms)
        self.assertIn("futuro 3", terms)
        self.assertNotIn("futuro 4", terms)  # solo los primeros 3

    def test_filters_short_terms(self):
        """Términos con <= 3 caracteres deben filtrarse."""
        structured = {
            "contributions": ["ab", "un término válido aquí"],
        }
        terms = extract_structured_terms(structured)
        self.assertNotIn("ab", terms)
        self.assertIn("un término válido aquí", terms)

    def test_deduplicates_and_limits_to_80(self):
        """No debe devolver duplicados y debe limitar a 80 términos."""
        structured = {
            "contributions": ["término duplicado"] * 10,
        }
        terms = extract_structured_terms(structured)
        self.assertEqual(terms.count("término duplicado"), 1)
        self.assertLessEqual(len(terms), 80)

    def test_high_value_terms_prioritized(self):
        """key_numbers y metrics deben aparecer entre los términos extraídos."""
        structured = {
            "contributions": ["contribución genérica"],
            "results": {
                "key_numbers": ["99.9% precisión"],
            },
            "methodology": {
                "metrics": ["exactitud"],
                "datasets": ["dataset X"],
                "baselines": ["baseline Y"],
                "signal_modalities": ["señal Z"],
            },
        }
        terms = extract_structured_terms(structured)
        # Los términos de alto valor deben estar presentes
        self.assertIn("99.9% precisión", terms)
        self.assertIn("exactitud", terms)
        self.assertIn("dataset X", terms)
        self.assertIn("baseline Y", terms)
        self.assertIn("señal Z", terms)

    def test_clean_terms_from_long_strings(self):
        """Frases largas se descomponen en n-grams técnicos."""
        structured = {
            "contributions": [
                "proponemos un nuevo sistema de detección de anomalías en señales ECG"
            ],
        }
        terms = extract_structured_terms(structured)
        # Debe haber términos descompuidos (no solo la frase completa)
        self.assertTrue(any("detección" in t or "anomalías" in t for t in terms))


class TestBuildEnrichedText(unittest.TestCase):
    """Pruebas para build_enriched_text."""

    def test_returns_text_from_clean_text(self):
        """Si solo hay clean_text (y tiene >500 chars), debe incluirlo."""
        paper = {"clean_text": "Este es el texto limpio del paper. " * 30}
        result = build_enriched_text(paper)
        self.assertIn("Este es el texto limpio del paper", result)

    def test_truncates_long_clean_text(self):
        """clean_text muy largo debe truncarse según max_section_chars."""
        long_text = "x" * 20000
        paper = {"clean_text": long_text}
        result = build_enriched_text(paper, max_section_chars=1000)
        # clean_text se agrega con *2, así que 2000 chars
        self.assertLessEqual(len(result), 2000 + 100)  # margen por espacios

    def test_adds_section_texts(self):
        """Debe agregar secciones methodology, results, conclusion, etc."""
        paper = {
            "clean_text": "",
            "clean_sections": {
                "methodology": "Descripción de la metodología propuesta. " * 10,
                "results": "Los resultados muestran mejoras significativas. " * 10,
                "introduction": "Introducción al tema de investigación. " * 10,
            },
        }
        result = build_enriched_text(paper)
        self.assertIn("Descripción de la metodología propuesta", result)
        self.assertIn("Los resultados muestran mejoras", result)
        self.assertIn("Introducción al tema de investigación", result)

    def test_adds_structured_contributions(self):
        """Debe agregar sección virtual CONTRIBUTIONS."""
        paper = {"clean_text": ""}
        structured = {
            "contributions": ["contribución A", "contribución B"],
        }
        result = build_enriched_text(paper, structured=structured)
        self.assertIn("CONTRIBUTIONS:", result)
        self.assertIn("contribución A", result)
        self.assertIn("contribución B", result)

    def test_adds_structured_methodology(self):
        """Debe agregar sección virtual METHODOLOGY_STRUCTURED."""
        paper = {"clean_text": ""}
        structured = {
            "methodology": {
                "approach": "enfoque propuesto",
                "architecture": "red neuronal",
                "datasets": ["ds1", "ds2"],
                "metrics": ["accuracy", "F1"],
            }
        }
        result = build_enriched_text(paper, structured=structured)
        self.assertIn("METHODOLOGY_STRUCTURED:", result)
        self.assertIn("approach: enfoque propuesto", result)
        self.assertIn("architecture: red neuronal", result)
        self.assertIn("datasets: ds1, ds2", result)
        self.assertIn("metrics: accuracy, F1", result)

    def test_adds_structured_results(self):
        """Debe agregar sección virtual RESULTS_STRUCTURED."""
        paper = {"clean_text": ""}
        structured = {
            "results": {
                "main": "resultado principal",
                "ablation": ["exp A", "exp B"],
                "key_numbers": ["95% accuracy"],
            }
        }
        result = build_enriched_text(paper, structured=structured)
        self.assertIn("RESULTS_STRUCTURED:", result)
        self.assertIn("main: resultado principal", result)
        self.assertIn("ablation: exp A; exp B", result)
        self.assertIn("key_numbers: 95% accuracy", result)

    def test_adds_takeaway(self):
        """Debe agregar TAKEAWAY si existe."""
        paper = {"clean_text": ""}
        structured = {"takeaway": "Este es el takeaway."}
        result = build_enriched_text(paper, structured=structured)
        self.assertIn("TAKEAWAY: Este es el takeaway.", result)

    def test_adds_argumentative_zones(self):
        """Debe agregar párrafos de zonas CLAIM, EVIDENCE, METHOD con confianza > 0.3."""
        paper = {"clean_text": ""}
        zoned = {
            "paragraphs": [
                {"zone": "claim", "text": "Este es un claim importante.", "confidence": 0.8},
                {"zone": "evidence", "text": "Evidencia numérica aquí.", "confidence": 0.9},
                {"zone": "method", "text": "Método detallado.", "confidence": 0.7},
                {"zone": "background", "text": "Fondo contextual.", "confidence": 0.8},
                {"zone": "claim", "text": "Claim de baja confianza.", "confidence": 0.2},
            ],
        }
        result = build_enriched_text(paper, zoned=zoned)
        self.assertIn("ARGUMENTATIVE_ZONES:", result)
        self.assertIn("[CLAIM] Este es un claim importante.", result)
        self.assertIn("[EVIDENCE] Evidencia numérica aquí.", result)
        self.assertIn("[METHOD] Método detallado.", result)
        self.assertNotIn("[BACKGROUND]", result)  # zona no prioritaria
        self.assertNotIn("baja confianza", result)  # confianza <= 0.3

    def test_skipped_structured_not_added(self):
        """Si structured tiene skipped=True, no debe agregar secciones virtuales."""
        paper = {"clean_text": ""}
        structured = {"skipped": True, "contributions": ["no debe aparecer"]}
        result = build_enriched_text(paper, structured=structured)
        self.assertNotIn("CONTRIBUTIONS:", result)
        self.assertNotIn("no debe aparecer", result)

    def test_max_section_chars_respected(self):
        """max_section_chars debe limitar la longitud de cada sección."""
        paper = {
            "clean_sections": {
                "methodology": "x" * 5000,
                "results": "y" * 5000,
            }
        }
        result = build_enriched_text(paper, max_section_chars=1000)
        # Cada sección debe estar truncada a 1000 chars
        self.assertLessEqual(len(result), 5000)  # margen generoso


class TestLoadStructuredPapers(unittest.TestCase):
    """Pruebas para load_structured_papers."""

    def test_returns_empty_if_file_missing(self):
        """Si no existe el archivo, devuelve dict vacío."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = load_structured_papers(Path(tmpdir))
            self.assertEqual(result, {})

    def test_loads_list_format(self):
        """Debe cargar lista de papers y indexar por paper_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data = [
                {"paper_id": "p1", "title": "Paper 1"},
                {"paper_id": "p2", "title": "Paper 2"},
            ]
            path = Path(tmpdir) / "papers_structured.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            result = load_structured_papers(Path(tmpdir))
            self.assertIn("p1", result)
            self.assertIn("p2", result)
            self.assertEqual(result["p1"]["title"], "Paper 1")

    def test_handles_invalid_json(self):
        """Si el JSON es inválido, devuelve vacío."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "papers_structured.json"
            path.write_text("no es json válido", encoding="utf-8")
            result = load_structured_papers(Path(tmpdir))
            self.assertEqual(result, {})


class TestLoadZonedPapers(unittest.TestCase):
    """Pruebas para load_zoned_papers."""

    def test_returns_empty_if_file_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = load_zoned_papers(Path(tmpdir))
            self.assertEqual(result, {})

    def test_loads_list_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data = [
                {"paper_id": "z1", "paragraphs": [{"zone": "claim", "text": "claim text"}]},
            ]
            path = Path(tmpdir) / "papers_zoned.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            result = load_zoned_papers(Path(tmpdir))
            self.assertIn("z1", result)
            self.assertEqual(len(result["z1"]["paragraphs"]), 1)


class TestDeduplicateSemanticTerms(unittest.TestCase):
    """Pruebas para deduplicate_semantic_terms."""

    def test_empty_list(self):
        """Lista vacía devuelve vacía."""
        self.assertEqual(deduplicate_semantic_terms([]), [])

    def test_single_term(self):
        """Un solo término devuelve ese término."""
        self.assertEqual(deduplicate_semantic_terms(["machine learning"]), ["machine learning"])

    def test_no_duplicates_keeps_all(self):
        """Términos semánticamente distintos se mantienen todos."""
        terms = ["machine learning", "climate change", "quantum computing", "blockchain"]
        with patch("sources.conceptMiningEngine._get_semantic_model") as mock_get_model:
            mock_model = MagicMock()
            # Simular embeddings ortogonales (similitud 0 entre todos)
            embeddings = np.eye(len(terms))
            mock_model.encode.return_value = embeddings
            mock_get_model.return_value = mock_model

            result = deduplicate_semantic_terms(terms, threshold=0.85)
            self.assertEqual(len(result), len(terms))
            for t in terms:
                self.assertIn(t, result)

    def test_removes_semantic_duplicates(self):
        """Términos muy similares semánticamente se reducen al primero (mayor prioridad)."""
        terms = ["machine learning", "machine learning model", "deep learning", "climate change"]
        with patch("sources.conceptMiningEngine._get_semantic_model") as mock_get_model:
            mock_model = MagicMock()
            # Embeddings: "machine learning" y "machine learning model" son muy similares (0.95)
            # "deep learning" es medio similar a "machine learning" (0.7)
            # "climate change" es ortogonal a todo
            emb_ml = np.array([1.0, 0.0, 0.0])
            emb_ml_model = np.array([0.95, 0.1, 0.0])
            emb_dl = np.array([0.7, 0.0, 0.0])
            emb_cc = np.array([0.0, 1.0, 0.0])
            embeddings = np.vstack([emb_ml, emb_ml_model, emb_dl, emb_cc])
            mock_model.encode.return_value = embeddings
            mock_get_model.return_value = mock_model

            result = deduplicate_semantic_terms(terms, threshold=0.85)
            # "machine learning" (prioridad 0) se mantiene
            # "machine learning model" (prioridad 1) es duplicado de 0 (0.95 >= 0.85) → se elimina
            # "deep learning" (prioridad 2) NO es duplicado (0.7 < 0.85) → se mantiene
            # "climate change" (prioridad 3) se mantiene
            self.assertIn("machine learning", result)
            self.assertNotIn("machine learning model", result)
            self.assertIn("deep learning", result)
            self.assertIn("climate change", result)
            self.assertEqual(len(result), 3)

    def test_custom_threshold(self):
        """Umbral personalizado afecta qué se considera duplicado."""
        terms = ["machine learning", "machine learning model"]
        with patch("sources.conceptMiningEngine._get_semantic_model") as mock_get_model:
            mock_model = MagicMock()
            emb_ml = np.array([1.0, 0.0])
            emb_ml_model = np.array([0.9, 0.1])
            embeddings = np.vstack([emb_ml, emb_ml_model])
            mock_model.encode.return_value = embeddings
            mock_get_model.return_value = mock_model

            # Umbral alto (0.95): no son duplicados
            result_high = deduplicate_semantic_terms(terms, threshold=0.95)
            self.assertEqual(len(result_high), 2)

            # Umbral bajo (0.85): sí son duplicados
            result_low = deduplicate_semantic_terms(terms, threshold=0.85)
            self.assertEqual(len(result_low), 1)
            self.assertEqual(result_low[0], "machine learning")

    def test_preserves_priority_order(self):
        """El término con mayor prioridad (primero en lista) se mantiene."""
        terms = ["neural network", "neural networks", "deep learning"]
        with patch("sources.conceptMiningEngine._get_semantic_model") as mock_get_model:
            mock_model = MagicMock()
            emb_nn = np.array([1.0, 0.0])
            emb_nns = np.array([0.98, 0.02])  # muy similar a nn
            emb_dl = np.array([0.5, 0.0])  # no es duplicado
            embeddings = np.vstack([emb_nn, emb_nns, emb_dl])
            mock_model.encode.return_value = embeddings
            mock_get_model.return_value = mock_model

            result = deduplicate_semantic_terms(terms, threshold=0.90)
            # "neural network" (primero) se mantiene
            # "neural networks" (0.98 >= 0.90) se elimina
            # "deep learning" se mantiene
            self.assertEqual(result[0], "neural network")
            self.assertNotIn("neural networks", result)
            self.assertIn("deep learning", result)


if __name__ == "__main__":
    unittest.main()
