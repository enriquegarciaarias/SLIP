import unittest

import numpy as np

from sources.discoveryEngine import deduplicate_concepts


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


if __name__ == "__main__":
    unittest.main()
