import unittest

import numpy as np

from sources.discoveryEngine import deduplicate_concepts, POSFilterVectorizer, nlp
from sources.conceptAlignment import select_best_objective, add_document_level_fallback


class _FakeEncoder:
    """Encoder determinista (bag-of-words hasheado y normalizado)."""

    def encode(self, texts, normalize_embeddings=True):
        if isinstance(texts, str):
            texts = [texts]
        dim = 64
        matrix = np.zeros((len(texts), dim))
        for row, text in enumerate(texts):
            for word in text.lower().split():
                matrix[row, hash(word) % dim] += 1
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return matrix / norms


class TestConceptDedup(unittest.TestCase):
    def test_merges_similar_and_remaps_doc_topic_map(self):
        concepts = [
            {"concept_id": 0, "label": "ai inclusive education",
             "document_indices": ["d1", "d2"], "n_chunks": 2,
             "keywords": ["a"], "study_similarity": 0.7},
            {"concept_id": 100, "label": "ai inclusive education",
             "document_indices": ["d2", "d3"], "n_chunks": 1,
             "keywords": ["b"], "study_similarity": 0.6},
            {"concept_id": 200, "label": "eeg emotion",
             "document_indices": ["d4"], "n_chunks": 1,
             "keywords": ["c"], "study_similarity": 0.8},
        ]
        doc_topic_map = [
            {"doc_id": "d1", "topic": 0, "section": "i"},
            {"doc_id": "d2", "topic": 100, "section": "i"},
            {"doc_id": "d4", "topic": 200, "section": "i"},
        ]
        kept, remapped = deduplicate_concepts(
            concepts, doc_topic_map, _FakeEncoder(), threshold=0.9
        )
        self.assertEqual(len(kept), 2)
        self.assertEqual(set(kept[0]["document_indices"]), {"d1", "d2", "d3"})
        self.assertTrue(all(mapping["topic"] in (0, 200) for mapping in remapped))


class _FakeModel:
    """Encoder determinista (bag-of-words hasheado) que acepta kwargs."""

    def encode(self, texts, **kwargs):
        if isinstance(texts, str):
            texts = [texts]
        dim = 128
        matrix = np.zeros((len(texts), dim))
        for row, text in enumerate(texts):
            for word in text.lower().split():
                matrix[row, hash(word) % dim] += 1
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return matrix / norms


class TestDocLevelFallback(unittest.TestCase):
    def test_select_best_objective_respects_threshold(self):
        self.assertEqual(
            select_best_objective(np.array([0.2, 0.8]), [0, 1], 0.45), (1, 0.8)
        )
        self.assertIsNone(
            select_best_objective(np.array([0.2, 0.3]), [0, 1], 0.45)
        )

    def test_assigns_uncovered_doc_to_most_similar_concept(self):
        aligned = [
            {"concept_id": 0, "documents": [], "document_scores": [], "n_documents": 0},
            {"concept_id": 1, "documents": [], "document_scores": [], "n_documents": 0},
        ]
        corpus = [
            {"paper_id": "p1", "title": "eeg emotion stress detection",
             "clean_text": "eeg emotion stress detection classification"},
            {"paper_id": "p2", "title": "cooking tomatoes",
             "clean_text": "cooking tomatoes basil garlic recipe"},
        ]
        objective = {"concepts": [
            {"id": 0, "name": "eeg emotion",
             "query": "eeg based emotion", "description": "physiological emotion recognition"},
            {"id": 1, "name": "weather forecast",
             "query": "weather forecasting", "description": "climate prediction models"},
        ]}
        added = add_document_level_fallback(
            aligned, corpus, objective, _FakeModel(),
            focus_terms=[], threshold=0.3,
        )
        added_ids = {pid for pid, _, _ in added}
        self.assertIn("p1", added_ids)
        self.assertEqual(aligned[0]["documents"][0]["doc_id"], "p1")
        self.assertNotIn("p2", added_ids)

    def test_skips_docs_already_covered_by_topics(self):
        aligned = [
            {"concept_id": 0, "documents": [{"doc_id": "p1"}],
             "document_scores": [0.9], "n_documents": 1},
            {"concept_id": 1, "documents": [], "document_scores": [], "n_documents": 0},
        ]
        corpus = [{"paper_id": "p1", "title": "eeg", "clean_text": "eeg"}]
        objective = {"concepts": [
            {"id": 0, "name": "eeg", "query": "eeg", "description": "eeg"},
            {"id": 1, "name": "x", "query": "x", "description": "x"},
        ]}
        added = add_document_level_fallback(
            aligned, corpus, objective, _FakeModel(),
            focus_terms=[], threshold=0.0,
        )
        self.assertEqual(added, [])


class TestVectorizerSmallCorpus(unittest.TestCase):
    def test_fit_on_one_or_two_docs_does_not_raise(self):
        """BERTopic ajusta el vectorizador sobre 1 fila por topic; con <=2
        topics, `max_df * n_doc < min_df` hacía abortar a sklearn."""
        for docs in (
            ["artificial intelligence language learning"],
            ["artificial intelligence language learning",
             "artificial intelligence higher education barriers"],
        ):
            vectorizer = POSFilterVectorizer(
                nlp_model=nlp, ngram_range=(1, 2), min_df=2, max_df=0.85
            )
            matrix = vectorizer.fit_transform(docs)
            self.assertEqual(matrix.shape[0], len(docs))


if __name__ == "__main__":
    unittest.main()
