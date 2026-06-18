from sources.common.common import logger, processControl, writeLog

import math

from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer
import json
from pathlib import Path

MODEL_NAME = (
    "sentence-transformers/"
    "paraphrase-multilingual-mpnet-base-v2"
)

model = SentenceTransformer(MODEL_NAME)

def build_document_text(paper):

    title = paper.get("title", "")
    abstract = paper.get("abstract", "")

    keywords = " ".join(
        paper.get("keywords", [])
    )

    return f"""
    TITLE:
    {title}

    ABSTRACT:
    {abstract}

    KEYWORDS:
    {keywords}
    """

def rank_papers_embeddings(
    papers,
    study_description
):

    documents = [
        build_document_text(p)
        for p in papers
    ]

    query_embedding = model.encode(
        study_description,
        normalize_embeddings=True
    )

    document_embeddings = model.encode(
        documents,
        normalize_embeddings=True,
        show_progress_bar=True
    )

    similarities = cosine_similarity(
        [query_embedding],
        document_embeddings
    )[0]

    ranked = []

    for paper, score in zip(
        papers,
        similarities
    ):

        ranked.append({
            **paper,
            "relevance_score": float(score)
        })

    ranked.sort(
        key=lambda x: x["relevance_score"],
        reverse=True
    )

    return ranked

def select_top_k(ranked_papers, k=100):
    return ranked_papers[:k]

def processRankingEngine():
    writeLog("info", logger, "🚀 [START] Processing processRankingEngine")
    base_input_dir = Path(processControl.env.get("input", ""))
    base_output_dir = Path(processControl.env.get("output", ""))
    subject = processControl.args.subject

    # Directorios con subject
    input_dir = base_input_dir / subject
    output_dir = base_output_dir / subject
    canonical_file = (output_dir / "canonical.json")
    with open(canonical_file, "r", encoding="utf-8") as f:
        papers = json.load(f)
    study_description_file = (input_dir / "studyDescription.txt")

    with open( study_description_file, "r", encoding="utf-8" ) as f:
        study_description = f.read().strip()
    ranked = rank_papers_embeddings(papers, study_description)
    top100 = select_top_k(
        ranked,
        k=100
    )
    output_file = (output_dir / "ranked_papers.json")

    with open(output_file, "w", encoding="utf-8" ) as f:
        json.dump( top100, f, indent=2, ensure_ascii=False)

    return top100

if __name__ == "__main__":
    processRankingEngine()