import json
import tempfile
import unittest
from pathlib import Path

from sources.common.pdfIndex import (
    load_pdf_index,
    register_pdf,
    resolve_paper_pdf,
    write_pdf_log,
)
from sources.common.referenceFilter import looks_like_reference
from sources.common.utils import normalize_doi


class TestNormalizeDoi(unittest.TestCase):
    def test_strips_wos_date_suffix(self):
        self.assertEqual(
            normalize_doi("10.1007/978-3-032-03870-8_35 D3 2025-11-27"),
            "10.1007/978-3-032-03870-8_35",
        )

    def test_strips_resolver_prefix_and_trailing_punctuation(self):
        self.assertEqual(
            normalize_doi("https://doi.org/10.3389/feduc.2025.1554314."),
            "10.3389/feduc.2025.1554314",
        )

    def test_empty(self):
        self.assertEqual(normalize_doi(""), "")


class TestReferenceFilter(unittest.TestCase):
    def test_detects_reference_list(self):
        text = (
            "Zhang, J., & Zhang, Z. (2024). AI in Teacher Education. Journal of "
            "Computer Assisted Learning, 40, 1871-1885. doi:10.1234/abc. Vol. 40, "
            "pp. 1-20. Proceedings of the conference."
        )
        self.assertTrue(looks_like_reference(text))

    def test_plain_sentence_is_not_reference(self):
        self.assertFalse(
            looks_like_reference("Adaptive learning improves outcomes for students.")
        )


class TestPdfIndex(unittest.TestCase):
    def test_roundtrip_and_resolution(self):
        out = Path(tempfile.mkdtemp())
        (out / "pdfs").mkdir()
        (out / "pdfs" / "wos_1.pdf").write_bytes(b"%PDF-1.4")

        index, entries = {}, []
        register_pdf(index, entries, "wos_1", out / "pdfs" / "wos_1.pdf", "auto", out)
        write_pdf_log(out, index, entries)

        loaded = load_pdf_index(out)
        self.assertIn("wos_1", loaded)
        self.assertEqual(
            resolve_paper_pdf({"paper_id": "wos_1"}, out, loaded),
            out / "pdfs" / "wos_1.pdf",
        )
        # Respaldo por local_pdf_path
        self.assertEqual(
            resolve_paper_pdf(
                {"paper_id": "x", "local_pdf_path": str(out / "pdfs" / "wos_1.pdf")},
                out,
                loaded,
            ),
            out / "pdfs" / "wos_1.pdf",
        )

    def test_legacy_list_format(self):
        out = Path(tempfile.mkdtemp())
        (out / "pdf_download_log.json").write_text(
            json.dumps([{"paper_id": "b", "pdf_file": "pdfs/b.pdf", "type": "auto"}]),
            encoding="utf-8",
        )
        self.assertIn("b", load_pdf_index(out))


if __name__ == "__main__":
    unittest.main()
