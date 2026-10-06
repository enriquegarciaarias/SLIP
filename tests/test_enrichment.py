import unittest

from sources.enrichmentEngine import refresh_selection_from_review


class TestRefreshSelection(unittest.TestCase):
    def test_refreshes_payload_and_drops_stale(self):
        review = [
            {"paper_id": "wos_1", "doi": "10.1/new", "selected": False},
            {"paper_id": "wos_2", "doi": "10.2/x", "selected": False},
        ]
        selected = [{"paper_id": "wos_1", "doi": "10.1/old", "selected": True}]
        rejected = [{"paper_id": "gone", "doi": "9"}]

        new_selected, new_rejected, stale = refresh_selection_from_review(
            selected, rejected, review
        )

        # El payload viene del ranking actual (DOI corregido), no del checkpoint.
        self.assertEqual([p["doi"] for p in new_selected], ["10.1/new"])
        self.assertTrue(new_selected[0]["selected"])
        # El id que ya no está en el ranking se descarta.
        self.assertEqual(stale, ["gone"])
        self.assertEqual(new_rejected, [])
        # No se muta el ranking de entrada de forma inesperada.
        self.assertFalse(review[1]["selected"])


if __name__ == "__main__":
    unittest.main()
