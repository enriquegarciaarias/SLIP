from sources.common.common import logger, processControl, writeLog
from pathlib import Path
import json

import numpy as np
import spacy

from sentence_transformers import SentenceTransformer
from rank_bm25 import BM25Okapi



# --------------------------------------------------
# CONFIG
# --------------------------------------------------

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

SIMILARITY_THRESHOLD = 0.35

MAX_EVIDENCES_PER_CONCEPT = 50


# --------------------------------------------------
# MODELS
# --------------------------------------------------

nlp = spacy.load("en_core_web_sm")

embedding_model = SentenceTransformer(
    EMBEDDING_MODEL
)

# --------------------------------------------------
# HELPERS
# --------------------------------------------------
def is_noise_sentence(sentence):
    bad_patterns = [
        "proceedings",
        "association for computational linguistics",
        "abstract",
        "introduction",
        "doi",
        "http",
        "www",
        "pages"
    ]

    s = sentence.lower()

    return any(p in s for p in bad_patterns)

def is_claim_score(sentence):
    patterns = [
        "result", "find", "show", "demonstrate",
        "evaluate", "outperform", "fail", "improve"
    ]

    text = sentence.lower()
    return sum(p in text for p in patterns)

def cosine_similarity(a, b):
    return float(
        np.dot(a, b) /
        (
            np.linalg.norm(a)
            * np.linalg.norm(b)
            + 1e-12
        )
    )

def build_retrieval_query(concept):

    parts = []
    if concept.get("concept_name"):
        parts.append(concept["concept_name"])
    if concept.get("concept_query"):
        parts.append(concept["concept_query"])
    if concept.get("concept_description"):
        parts.append(concept["concept_description"])
    return " ".join(parts)

def sentence_split(text):
    if not text:
        return []
    doc = nlp(text)
    return [
        sent.text.strip()
        for sent in doc.sents
        if len(sent.text.strip()) > 40
    ]

def bm25_tokenize(text):
    return [
        token.lower()
        for token in text.split()
    ]


def extract_candidate_sentences(document_text):
    sentences = sentence_split(document_text)
    return [
        s for s in sentences
        if not is_noise_sentence(s)
    ]

def rank_sentences(concept_query,  candidate_sentences):
    if not candidate_sentences:
        return []

    query_embedding = embedding_model.encode(
        concept_query,
        normalize_embeddings=True
    )

    sentence_embeddings = embedding_model.encode(
        candidate_sentences,
        normalize_embeddings=True
    )

    embedding_scores = []

    for emb in sentence_embeddings:

        embedding_scores.append(
            cosine_similarity(
                query_embedding,
                emb
            )
        )

    # ----------------------------------
    # BM25 SCORES
    # ----------------------------------

    tokenized_corpus = [
        bm25_tokenize(s)
        for s in candidate_sentences
    ]

    bm25 = BM25Okapi(
        tokenized_corpus
    )

    tokenized_query = bm25_tokenize(
        concept_query
    )

    bm25_scores = bm25.get_scores(
        tokenized_query
    )

    # ----------------------------------
    # NORMALIZATION
    # ----------------------------------

    bm25_scores = np.array(
        bm25_scores,
        dtype=float
    )

    if bm25_scores.max() > 0:

        bm25_scores = (
            bm25_scores -
            bm25_scores.min()
        ) / (
            bm25_scores.max() -
            bm25_scores.min() +
            1e-12
        )

    # ----------------------------------
    # HYBRID SCORE
    # ----------------------------------

    results = []
    for sentence, emb_score, bm_score in zip(
            candidate_sentences,
            embedding_scores,
            bm25_scores
    ):
        claim_score = is_claim_score(sentence)

        hybrid_score = (
                0.7 * emb_score +
                0.3 * bm_score
        )

        final_score = hybrid_score + 0.1 * claim_score

        results.append({
            "sentence": sentence,
            "embedding_score": float(emb_score),
            "bm25_score": float(bm_score),
            "claim_score": float(claim_score),
            "score": float(final_score)
        })

    results.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return results

def extract_evidence_from_document(concept_query,  document):

    candidates = extract_candidate_sentences(
        document["text"]
    )

    ranked = rank_sentences(
        concept_query,
        candidates
    )

    return ranked

# --------------------------------------------------

def process_concept(concept):
    evidences = []
    for document in concept["documents"]:
        retrieval_query = build_retrieval_query(concept)
        ranked_sentences = extract_evidence_from_document(retrieval_query, document)
        for item in ranked_sentences:
            evidences.append({
                "doc_id":
                    document["doc_id"],
                "paper_title":
                    document["title"],
                "claim":
                    item["sentence"],
                "evidence_text":
                    item["sentence"],

                "similarity":
                    round(item["score"], 4)
            })

    evidences.sort(
        key=lambda x: x["similarity"],
        reverse=True
    )

    evidences = [
        e for e in evidences
        if e["similarity"] >= SIMILARITY_THRESHOLD
    ]

    evidences = evidences[
        :MAX_EVIDENCES_PER_CONCEPT
    ]

    return {
        "concept_id":
            concept["concept_id"],

        "concept_query":
            concept["concept_query"],

        "knowledge_units":
            evidences
    }

# --------------------------------------------------

def build_evidence_layer(aligned_concepts):

    concepts_output = []

    for concept in aligned_concepts["aligned_concepts"]:

        writeLog(
            "info",
            logger,
            f"[Evidence] Processing concept {concept['concept_id']}"
        )

        concepts_output.append(
            process_concept(concept)
        )

    return {
        "schema_version": "1.0",
        "concepts": concepts_output
    }

# --------------------------------------------------

def save_output(result, output_file):

    with open(
        output_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            result,
            f,
            indent=2,
            ensure_ascii=False
        )

# --------------------------------------------------

def processConceptEvidence():

    output_dir = Path(
        processControl.env.get(
            "output",
            ""
        )
    )

    input_file = (
        output_dir /
        "aligned_concepts.json"
    )

    with open(
        input_file,
        "r",
        encoding="utf-8"
    ) as f:

        aligned_concepts = json.load(f)

    writeLog(
        "info",
        logger,
        "[Evidence] Extracting evidence..."
    )

    result = build_evidence_layer(
        aligned_concepts
    )

    output_file = (
        output_dir /
        "concept_evidence.json"
    )

    save_output(
        result,
        output_file
    )

    writeLog(
        "info",
        logger,
        f"[Evidence] Saved to {output_file}"
    )

    return result

# --------------------------------------------------

if __name__ == "__main__":
    processConceptEvidence()