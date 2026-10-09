import json
import os
import tempfile
import unittest

from sources.ingestion.base import QueryResolver
from sources.ingestion.manager import _dedup_records
from sources.ingestion.openalex_provider import OpenAlexProvider
from sources.ingestion.pubmed_provider import PubmedProvider

_PUBMED_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>39981353</PMID>
      <Article>
        <ArticleTitle>Assessing zero-shot VQA for ECG interpretation</ArticleTitle>
        <Journal><Title>Front Cardiovasc Med</Title></Journal>
      </Article>
      <ReferenceList>
        <Reference>
          <ArticleIdList>
            <ArticleId IdType="doi">10.1111/1365-2656.12382</ArticleId>
            <ArticleId IdType="pmc">PMC99999999</ArticleId>
          </ArticleIdList>
        </Reference>
      </ReferenceList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">39981353</ArticleId>
        <ArticleId IdType="doi">10.3389/fcvm.2025.1458289</ArticleId>
        <ArticleId IdType="pmc">PMC11839599</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>"""


class TestQueryResolver(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "conceptsQuery.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "query": "broad",
                    "concepts": [
                        {"id": 0, "name": "A", "query": "rq one"},
                        {"id": 1, "name": "B", "query": "rq two"},
                        {"id": 2, "name": "C"},
                    ],
                },
                fh,
            )
        self.resolver = QueryResolver(self.dir, {"query_source": "conceptsQuery.json"})

    def test_for_source_fallback(self):
        self.assertEqual(
            self.resolver.for_source("pubmed"), ("broad", "conceptsQuery.json:query")
        )

    def test_per_concept_queries(self):
        self.assertEqual(
            self.resolver.per_concept_queries("pubmed"),
            [("A", "rq one"), ("B", "rq two"), ("C", "C")],
        )


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, params=None, headers=None, accept_json=True):
        return self.payload


class TestOpenAlexProvider(unittest.TestCase):
    def test_normalize_reconstructs_abstract(self):
        payload = {
            "meta": {"count": 1},
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "doi": "https://doi.org/10.1/x",
                    "title": "T",
                    "publication_year": 2024,
                    "abstract_inverted_index": {"a": [1], "b": [0]},
                }
            ],
        }
        provider = OpenAlexProvider("openalex", {"email": "e"}, client=_FakeClient(payload))
        records = provider.normalize(provider.fetch("q", max_results=10))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["doi"], "10.1/x")
        self.assertEqual(records[0]["abstract"], "b a")


class TestDedupRecords(unittest.TestCase):
    def test_dedup_by_original_id(self):
        records = [
            {"original_id": "doi:10.1/a"},
            {"original_id": "doi:10.1/a"},
            {"original_id": "title:z"},
        ]
        self.assertEqual(len(_dedup_records(records)), 2)


class TestPubmedProviderIds(unittest.TestCase):
    def test_uses_own_article_ids_not_reference_ids(self):
        provider = PubmedProvider("pubmed", {})
        records = provider.normalize({"xml_batches": [_PUBMED_XML]})
        self.assertEqual(len(records), 1)
        record = records[0]
        # El DOI/PMCID deben ser los propios, no los de la última referencia.
        self.assertEqual(record["doi"], "10.3389/fcvm.2025.1458289")
        self.assertEqual(record["pmcid"], "PMC11839599")


if __name__ == "__main__":
    unittest.main()
