import os
import tempfile
import unittest

from sources.common.utils import needs_abstract_recovery
from sources.normalizeSearchResults import parse_acm_file, parse_arxiv_file
from sources.abstractRecovery import fetch_arxiv_abstracts, recover_abstracts


ACM_SAMPLE = (
    '"#" ; "Titulo" ; "DOI" ; "Abstract"\n'
    '1 ; "A Great Paper on Sensors" ; "https://doi.org/10.1145/1234.5678" ; '
    '"This is a truncated abstract that keeps going and going ..."\n'
    '2 ; "Full Abstract Paper" ; "https://doi.org/10.1145/9999.0001" ; '
    '"A complete abstract without truncation markers, long enough to pass filters."\n'
)

ARXIV_SAMPLE = (
    '"#" ; "arXiv_ID" ; "Titulo" ; "URL" ; "Abstract"\n'
    '1 ; "arXiv:2610.02866" ; "DIVINE: Pretraining" ; '
    '"https://arxiv.org/abs/2610.02866" ; "Financial time-\u2026"\n'
)


class TestNeedsAbstractRecovery(unittest.TestCase):
    def test_empty_and_elipsis(self):
        self.assertTrue(needs_abstract_recovery(""))
        self.assertTrue(needs_abstract_recovery("text \u2026"))
        self.assertTrue(needs_abstract_recovery("text ..."))

    def test_complete(self):
        self.assertFalse(needs_abstract_recovery("A complete abstract."))


class TestAcmParser(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(ACM_SAMPLE)

    def tearDown(self):
        os.unlink(self.path)

    def test_parses_and_flags_truncation(self):
        papers = parse_acm_file(self.path)
        self.assertEqual(len(papers), 2)
        first = papers[0]
        self.assertEqual(first["doi"], "10.1145/1234.5678")
        self.assertEqual(first["landing_page"], "https://doi.org/10.1145/1234.5678")
        self.assertTrue(first["abstract_truncated"])
        self.assertFalse(papers[1]["abstract_truncated"])


class TestArxivParser(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(ARXIV_SAMPLE)

    def tearDown(self):
        os.unlink(self.path)

    def test_parses_and_builds_pdf_url(self):
        papers = parse_arxiv_file(self.path)
        self.assertEqual(len(papers), 1)
        paper = papers[0]
        self.assertEqual(paper["arxiv_id"], "2610.02866")
        self.assertEqual(paper["pdf_url"], "https://arxiv.org/pdf/2610.02866")
        self.assertTrue(paper["abstract_truncated"])


class _FakeOpenAlexClient:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, url, params=None, accept_json=True):
        for key, abstract in self.mapping.items():
            if key in url:
                return {"abstract_inverted_index": abstract}
        raise RuntimeError("not found")


class TestRecovery(unittest.TestCase):
    def test_recover_acm_via_openalex(self):
        records = [
            {"doi": "10.1/x", "abstract": "short \u2026", "abstract_truncated": True},
            {"doi": "10.1/y", "abstract": "already full abstract here", "abstract_truncated": False},
        ]
        client = _FakeOpenAlexClient({"10.1/x": {"full": [1], "abstract": [0]}})
        cfg = {"abstract_recovery": {"enabled": True, "arxiv_fallback": False}}
        recovered = recover_abstracts(records, "acm", cfg, client=client)
        self.assertEqual(recovered, 1)
        self.assertEqual(records[0]["abstract"], "abstract full")
        self.assertFalse(records[0]["abstract_truncated"])
        self.assertTrue(records[0]["abstract_recovered"])

    def test_recover_arxiv_doi_mapping(self):
        records = [
            {"arxiv_id": "2610.02866", "doi": "", "abstract": "x \u2026", "abstract_truncated": True},
        ]
        client = _FakeOpenAlexClient({"10.48550/arXiv.2610.02866": {"ok": [0]}})
        cfg = {"abstract_recovery": {"enabled": True, "arxiv_fallback": False}}
        recovered = recover_abstracts(records, "arxiv", cfg, client=client)
        self.assertEqual(recovered, 1)
        self.assertEqual(records[0]["abstract"], "ok")

    def test_fetch_arxiv_abstracts_parses_atom(self):
        xml = (
            '<?xml version="1.0"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            '<entry><id>http://arxiv.org/abs/2610.02866v1</id>'
            '<summary>Full abstract text.</summary></entry>'
            '</feed>'
        )

        class _TextClient:
            def get(self, url, params=None, accept_json=True):
                return xml

        abstracts = fetch_arxiv_abstracts(["2610.02866"], _TextClient())
        self.assertEqual(abstracts.get("2610.02866"), "Full abstract text.")


if __name__ == "__main__":
    unittest.main()
