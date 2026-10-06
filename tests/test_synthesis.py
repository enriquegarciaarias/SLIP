import unittest

from sources.sythesizeFindings import FindingGenerator
from sources.trainingMaterials import MarkdownRenderer


class TestCitationExtraction(unittest.TestCase):
    def test_maps_and_drops_out_of_range(self):
        evidences = [{"doc_id": "wos_1"}, {"doc_id": "scopus_2"}]
        citations = FindingGenerator._extract_citations("A [1]. B [2]. C [9].", evidences)
        self.assertEqual(citations, {1: "wos_1", 2: "scopus_2"})

    def test_empty_text(self):
        self.assertEqual(FindingGenerator._extract_citations("", []), {})


class TestTechnicalAnnexRender(unittest.TestCase):
    def test_dynamic_columns_domain_independent(self):
        concepts = (
            {
                "concept_id": 0,
                "concept_name": "c",
                "papers": [
                    {"paper_id": "p1", "profile": {"foo_bar": "x", "limitations": "y"}}
                ],
            },
        )
        renderer = object.__new__(MarkdownRenderer)
        out = MarkdownRenderer._render_technical_annex(renderer, concepts)
        self.assertIn("Foo bar", out)
        self.assertIn("Limitations", out)
        self.assertNotIn("sensor_devices", out)
        self.assertIn("x", out)


if __name__ == "__main__":
    unittest.main()
