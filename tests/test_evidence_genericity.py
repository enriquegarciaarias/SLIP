import unittest

import numpy as np

from sources.conceptEvidence import (
    ConceptProcessor,
    DocumentEvidenceExtractor,
    Evidence,
    EvidenceConfig,
    DEFAULT_SECTION_EVIDENCE_WEIGHTS,
)


class _FakeRegistry:
    """Encoder bag-of-words hasheado y normalizado (determinista)."""

    def encode_batch(self, texts, normalize=True):
        if isinstance(texts, str):
            texts = [texts]
        dim = 128
        matrix = np.zeros((len(texts), dim))
        for row, text in enumerate(texts):
            for word in text.lower().split():
                matrix[row, hash(word) % dim] += 1
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        matrix = matrix / norms
        return matrix


def _processor(**overrides):
    cfg = EvidenceConfig(**overrides)
    return ConceptProcessor(
        extractor=None,
        clusterer=None,
        evidence_filter=None,
        query_cache=None,
        registry=_FakeRegistry(),
        config=cfg,
    )


_REPEATED = "artificial intelligence inclusive education"
_UNIQUE = "gamma delta epsilon zeta"


def _evidence(doc_id, text, similarity):
    return Evidence(
        doc_id=doc_id, paper_title=doc_id, evidence_text=text, similarity=similarity
    )


class TestGenericityPenalty(unittest.TestCase):
    def _sample(self):
        return [
            _evidence("A", _REPEATED, 0.9),
            _evidence("B", _REPEATED, 0.9),
            _evidence("C", _REPEATED, 0.9),
            _evidence("D", _UNIQUE, 0.4),
        ]

    def test_never_promotes_values(self):
        """La genericidad no altera el score (el filtro mantiene el alcance)."""
        proc = _processor(genericity_penalty=0.5)
        annotated = proc._annotate_genericity(self._sample())
        for before, after in zip(self._sample(), annotated):
            self.assertAlmostEqual(after.similarity, before.similarity)

    def test_repeated_text_is_generic(self):
        proc = _processor(genericity_penalty=0.5)
        annotated = proc._annotate_genericity(self._sample())
        by_doc = {e.doc_id: e for e in annotated}
        # El texto repetido en A/B/C es genérico; el único (D) no.
        for doc in ("A", "B", "C"):
            self.assertGreaterEqual(by_doc[doc].genericity, 0.6)
        self.assertLess(by_doc["D"].genericity, 0.6)

    def test_split_puts_repeated_in_consensus(self):
        proc = _processor(genericity_penalty=0.5, generic_threshold=0.6)
        annotated = proc._annotate_genericity(self._sample())
        generic, differential = proc._split_generic(annotated)
        self.assertEqual({e.doc_id for e in generic}, {"A", "B", "C"})
        self.assertEqual([e.doc_id for e in differential], ["D"])

    def test_consensus_groups_by_paper_count(self):
        proc = _processor(genericity_penalty=0.5, generic_threshold=0.6)
        annotated = proc._annotate_genericity(self._sample())
        generic, _ = proc._split_generic(annotated)
        consensus = proc._build_consensus(generic)
        self.assertEqual(len(consensus), 1)
        self.assertEqual(consensus[0]["n_papers"], 3)
        self.assertEqual(consensus[0]["doc_ids"], ["A", "B", "C"])

    def test_consensus_disabled_keeps_everything_differential(self):
        proc = _processor(consensus_enabled=False)
        annotated = proc._annotate_genericity(self._sample())
        generic, differential = proc._split_generic(annotated)
        self.assertEqual(generic, [])
        self.assertEqual(len(differential), 4)


class TestSectionWeights(unittest.TestCase):
    def test_section_weight_prefers_differential_sections(self):
        cfg = EvidenceConfig()
        extractor = DocumentEvidenceExtractor(
            registry=_FakeRegistry(), scorer=None, config=cfg
        )
        self.assertEqual(extractor._section_weight("results"), cfg.section_weights["results"])
        self.assertEqual(extractor._section_weight("Introduction"), cfg.section_weights["introduction"])
        self.assertGreater(
            extractor._section_weight("results"), extractor._section_weight("introduction")
        )
        self.assertEqual(extractor._section_weight("unknown-section"), cfg.default_section_weight)

    def test_default_section_weight_map(self):
        self.assertEqual(DEFAULT_SECTION_EVIDENCE_WEIGHTS["abstract"], 0.4)
        self.assertEqual(DEFAULT_SECTION_EVIDENCE_WEIGHTS["results"], 1.0)


if __name__ == "__main__":
    unittest.main()
