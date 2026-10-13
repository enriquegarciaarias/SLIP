import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from sources.pdfVaultReconcile import reconcile_pdf_vault


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class TestVaultReconcile(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "pdfs").mkdir()
        (self.dir / "_pdf_vault").mkdir()

        # PDF de trabajo y su copia en el vault, pero indexada con DOI erróneo.
        content = b"%PDF-1.4 fake content for rekey test"
        (self.dir / "pdfs" / "p1.pdf").write_bytes(content)
        old_vault_name = _sha1("doi:wrong") + ".pdf"
        (self.dir / "_pdf_vault" / old_vault_name).write_bytes(content)
        self.old_vault_name = old_vault_name

        (self.dir / "pdf_inventory.json").write_text(
            json.dumps({"doi:wrong": old_vault_name}), encoding="utf-8"
        )
        (self.dir / "canonical.json").write_text(
            json.dumps([{"paper_id": "p1", "global_id": "doi:correct"}]),
            encoding="utf-8",
        )

    def test_rekeys_content_to_correct_global_id(self):
        report = reconcile_pdf_vault(self.dir)
        self.assertEqual(report["rekeyed"][0]["paper_id"], "p1")

        inv = json.loads((self.dir / "pdf_inventory.json").read_text(encoding="utf-8"))
        target = _sha1("doi:correct") + ".pdf"
        self.assertEqual(inv.get("doi:correct"), target)
        self.assertNotIn("doi:wrong", inv)
        # El contenido sobrevive bajo la clave correcta.
        self.assertTrue((self.dir / "_pdf_vault" / target).exists())
        self.assertFalse((self.dir / "_pdf_vault" / self.old_vault_name).exists())

    def test_archives_root_pdf_when_not_in_vault(self):
        # Eliminar el fichero del vault para forzar el archivado.
        (self.dir / "_pdf_vault" / self.old_vault_name).unlink()
        report = reconcile_pdf_vault(self.dir)
        self.assertEqual(len(report["archived"]), 1)
        target = _sha1("doi:correct") + ".pdf"
        self.assertTrue((self.dir / "_pdf_vault" / target).exists())

    def test_dry_run_does_not_modify(self):
        reconcile_pdf_vault(self.dir, dry_run=True)
        inv = json.loads((self.dir / "pdf_inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(inv, {"doi:wrong": self.old_vault_name})
        self.assertTrue((self.dir / "_pdf_vault" / self.old_vault_name).exists())


if __name__ == "__main__":
    unittest.main()
