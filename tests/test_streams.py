import json
import os
import tempfile
import unittest

from sources.common.streams import load_streams, stream_sources, stream_names_for_source
from sources.ingestion.base import QueryResolver
from sources.ingestion.manager import _dedup_records
from sources.rankingEngine import _stream_ranking_query


class TestLoadStreams(unittest.TestCase):
    def test_legacy_default_when_no_streams(self):
        streams = load_streams({"availables": ["scopus", "ieee"], "max_results": 250})
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["name"], "default")
        self.assertEqual(streams[0]["sources"], ["scopus", "ieee"])
        self.assertEqual(streams[0]["max_results"], 250)

    def test_named_streams_parsed_and_disabled_skipped(self):
        cfg = {
            "availables": ["scopus"],
            "streams": {
                "physio": {"query": "physio q", "sources": ["scopus", "ieee"]},
                "nlp": {"query": "nlp q", "sources": ["openalex"], "top_k": 50,
                        "enabled": True},
                "off": {"query": "x", "enabled": False},
            },
        }
        streams = load_streams(cfg)
        self.assertEqual([s["name"] for s in streams], ["physio", "nlp"])
        self.assertEqual(streams[1]["top_k"], 50)
        self.assertEqual(stream_sources(streams), ["scopus", "ieee", "openalex"])
        self.assertEqual(stream_names_for_source(streams, "scopus"), ["physio"])


class TestStreamQueryResolution(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "conceptsQuery.json"), "w", encoding="utf-8") as fh:
            json.dump({"query": "global", "queries": {"scopus": "scopus syntax"}}, fh)
        self.resolver = QueryResolver(self.dir, {"query_source": "conceptsQuery.json"})

    def test_stream_query_overrides(self):
        stream = {"name": "nlp", "query": "nlp base", "queries": {"scopus": "nlp scopus"}}
        self.assertEqual(
            self.resolver.for_stream_source(stream, "scopus"),
            ("nlp scopus", "streams.nlp.queries.scopus"),
        )
        self.assertEqual(
            self.resolver.for_stream_source(stream, "openalex"),
            ("nlp base", "streams.nlp.query"),
        )

    def test_falls_back_to_legacy(self):
        stream = {"name": "default", "query": "", "queries": {}}
        self.assertEqual(
            self.resolver.for_stream_source(stream, "scopus"),
            ("scopus syntax", "conceptsQuery.json:queries.scopus"),
        )


class TestStreamDedup(unittest.TestCase):
    def test_merges_query_streams(self):
        records = [
            {"original_id": "doi:1", "query_stream": "physio"},
            {"original_id": "doi:1", "query_stream": "nlp"},
            {"original_id": "doi:2", "query_stream": "nlp"},
        ]
        deduped = _dedup_records(records)
        self.assertEqual(len(deduped), 2)
        first = next(r for r in deduped if r["original_id"] == "doi:1")
        self.assertEqual(first["query_streams"], ["nlp", "physio"])


class TestStreamRankingQuery(unittest.TestCase):
    def test_priority(self):
        self.assertEqual(
            _stream_ranking_query({"ranking_query": "r", "query": "q"}, "study"), "r"
        )
        self.assertEqual(_stream_ranking_query({"query": "q"}, "study"), "q")
        self.assertEqual(_stream_ranking_query({}, "study"), "study")


if __name__ == "__main__":
    unittest.main()
