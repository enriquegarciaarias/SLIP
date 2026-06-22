# enrichmentEngine.py
from sources.common.common import logger, processControl, writeLog
from sources.pdf_resolver import resolve_pdf_url
from sources.common.utils import inicioModulo

import json
import time
import requests
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from datetime import datetime
from deep_translator import GoogleTranslator

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

UNPAYWALL_EMAIL = "ega3646209@gmail.com"
TIMEOUT = 20
SLEEP = 0.2

translator = GoogleTranslator(source='auto', target='es')

MARGIN_LEFT = 40
MARGIN_RIGHT = 50
MARGIN_BOTTOM = 60
FONT = "Helvetica"
FONT_SIZE = 10
LINE_HEIGHT = 12


# --------------------------------------------------
# PDF HELPERS
# --------------------------------------------------

def wrap_text(text: str, font: str, size: int, max_width: float) -> list[str]:
    words = text.split()
    lines = []
    current = ""
    for w in words:
        test = f"{current} {w}".strip()
        if stringWidth(test, font, size) <= max_width:
            current = test
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def generate_review_pdf(review: list, output_path: Path, top_k: int = 100):
    c = canvas.Canvas(str(output_path), pagesize=A4)
    width, height = A4
    usable_width = width - MARGIN_LEFT - MARGIN_RIGHT
    y_top = height - 50
    y = y_top

    for idx, p in enumerate(review[:top_k], start=1):
        blocks = [
            f"{idx}. {p.get('title', '')}",
            f"Año: {p.get('year', '')}",
            f"Citas: {p.get('citation_count', 'N/A')}",
            "",
            p.get("abstract", "")[:2000],
        ]

        all_wrapped = [wrap_text(b, FONT, FONT_SIZE, usable_width) for b in blocks]
        total_lines = sum(len(x) for x in all_wrapped)
        required_h = total_lines * LINE_HEIGHT

        if y - required_h < MARGIN_BOTTOM:
            c.showPage()
            c.setFont(FONT, FONT_SIZE)
            y = y_top

        for wrapped in all_wrapped:
            for line in wrapped:
                c.drawString(MARGIN_LEFT, y, line)
                y -= LINE_HEIGHT
            y -= 8

        y -= 10

    c.save()


# --------------------------------------------------
# TRANSLATION
# --------------------------------------------------

def translate_to_es(text: str) -> str:
    if not text:
        return ""
    try:
        return translator.translate(text)
    except Exception:
        return text


def build_dual_review(review: list) -> tuple[list, list]:
    original_review = []
    spanish_review = []
    for p in review:
        original_review.append(p)
        p_es = dict(p)
        p_es["abstract"] = translate_to_es(p.get("abstract", ""))
        spanish_review.append(p_es)
    return original_review, spanish_review


# --------------------------------------------------
# CHECKPOINT
# --------------------------------------------------

def load_checkpoint(checkpoint_path: Path) -> dict:
    if checkpoint_path.exists():
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_index": 0, "selected": [], "rejected": [], "timestamp": None}


def save_checkpoint(checkpoint_path: Path, state: dict):
    state["timestamp"] = datetime.utcnow().isoformat()
    with open(checkpoint_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# --------------------------------------------------
# ENRICHMENT
# --------------------------------------------------

def enrich_openalex(doi: str) -> dict:
    """Fetches citation count, publication year and OA status from OpenAlex."""
    if not doi:
        return {}
    url = f"https://api.openalex.org/works/https://doi.org/{doi}"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return {}
        data = r.json()
    except Exception:
        return {}

    return {
        "openalex_id": data.get("id"),
        "citation_count": data.get("cited_by_count"),
        "publication_year": data.get("publication_year"),
        "is_oa": data.get("open_access", {}).get("is_oa"),
    }


def enrich_paper(paper: dict) -> dict:
    """
    Enriches a single paper with citation metadata and a resolved PDF URL.
    """
    if paper is None:
        writeLog("warning", logger, "[Enrich] Skipping None paper")
        return {}

    doi = paper.get("doi")
    paper_id = paper.get("paper_id", "unknown")

    if not doi:
        writeLog("warning", logger, f"[Enrich] [{paper_id}] No DOI: {paper.get('title', '')[:60]!r}")

    oa_meta = enrich_openalex(doi)

    title = paper.get("title", "")
    resolution = resolve_pdf_url(doi or "", title)

    return {
        **paper,
        "citation_count": oa_meta.get("citation_count"),
        "openalex_id": oa_meta.get("openalex_id"),
        "publication_year": oa_meta.get("publication_year"),
        "is_oa": oa_meta.get("is_oa"),
        "pdf_url": resolution["pdf_url"],
        "pdf_source": resolution["pdf_source"],
        "has_pdf": resolution["pdf_url"] is not None,
        "selected": False,
    }


def enrichment_engine(ranked_papers: list) -> list:
    enriched = []
    total = len(ranked_papers)

    for i, paper in enumerate(ranked_papers):
        # Verificar que paper no sea None
        if paper is None:
            writeLog("warning", logger, f"[Enrich] {i + 1}/{total} Skipping None paper")
            continue

        # Manejar título None
        title = paper.get('title')
        if title is None:
            title = "NO TITLE"
        else:
            title = title[:60]

        paper_id = paper.get('paper_id', 'unknown')

        writeLog("info", logger, f"[Enrich] {i + 1}/{total} [{paper_id}] {title}")

        try:
            enriched_paper = enrich_paper(paper)
            if enriched_paper is None:
                writeLog("warning", logger, f"[Enrich] {i + 1}/{total} [{paper_id}] enrich_paper returned None")
                enriched.append(paper)
            else:
                enriched.append(enriched_paper)
        except Exception as e:
            writeLog("error", logger, f"[Enrich] {i + 1}/{total} [{paper_id}] Error: {e}")
            enriched.append(paper)

        time.sleep(SLEEP)

    return enriched


# --------------------------------------------------
# REVIEW STRUCTURE
# --------------------------------------------------

def build_candidate_review(enriched_papers: list) -> list:
    review = [
        {
            "paper_id": p.get("paper_id"),
            "title": p.get("title"),
            "abstract": p.get("abstract"),
            "keywords": p.get("keywords"),
            "year": p.get("year"),
            "citation_count": p.get("citation_count"),
            "relevance_score": p.get("relevance_score"),
            "is_oa": p.get("is_oa"),
            "doi": p.get("doi"),
            "pdf_url": p.get("pdf_url"),
            "pdf_source": p.get("pdf_source"),
            "has_pdf": p.get("has_pdf", False),
            "selected": p.get("selected", False),
        }
        for p in enriched_papers
    ]

    review.sort(
        key=lambda x: (
            x.get("relevance_score", 0),
            x.get("citation_count") or 0,
        ),
        reverse=True,
    )
    return review


# --------------------------------------------------
# INTERACTIVE SELECTION
# --------------------------------------------------

def interactive_selection(review: list, output_dir: Path) -> tuple[list, list]:
    """
    Selección interactiva de papers.

    Opciones:
        Y = Seleccionar (marcar para adquirir PDF después)
        N = Rechazar (no se adquirirá PDF)
        S = Guardar progreso y salir (reanudar después)
        F = Finalizar (todos los restantes se marcan como seleccionados)
    """
    checkpoint_path = output_dir / "selection_checkpoint.json"
    state = load_checkpoint(checkpoint_path)

    selected = state["selected"]
    rejected = state["rejected"]
    start_idx = state["last_index"]

    # IDs ya seleccionados
    selected_ids = {p["paper_id"] for p in selected}

    writeLog("info", logger, f"[Selection] Resuming from index {start_idx}")
    writeLog("info", logger, f"[Selection] Already selected: {len(selected_ids)}")

    for i in range(start_idx, len(review)):
        p = review[i]

        # Saltar papers ya seleccionados
        if p["paper_id"] in selected_ids:
            writeLog("info", logger, f"[Selection] Skipping {p['paper_id']} (already selected)")
            continue

        print("\n" + "=" * 80)
        print(f"[{i + 1}] ID: {p['paper_id']}")
        print(f"Title: {p['title']}")
        print(f"Year: {p.get('year')}  |  Citations: {p.get('citation_count')}")
        print(f"PDF source: {p.get('pdf_source', 'not resolved')}")
        print("\n" + (p.get("abstract") or "")[:800])

        print("\n📌 Opciones:")
        print("   Y = Seleccionar (se adquirirá PDF después)")
        print("   N = Rechazar")
        print("   S = Guardar progreso y salir (reanudar después)")
        print("   F = Finalizar (todos los restantes se seleccionan)")

        choice = input("\n¿Opción? (Y/N/S/F): ").strip().lower()

        if choice == "y":
            p["selected"] = True
            selected.append(p)
            selected_ids.add(p["paper_id"])
        elif choice == "f":
            writeLog("info", logger, "[Selection] Finalizing: marking all remaining as selected")
            for j in range(i, len(review)):
                if review[j]["paper_id"] not in selected_ids:
                    review[j]["selected"] = True
                    selected.append(review[j])
                    selected_ids.add(review[j]["paper_id"])
            state.update({"last_index": len(review), "selected": selected, "rejected": rejected})
            save_checkpoint(checkpoint_path, state)
            break
        elif choice == "s":
            state.update({"last_index": i, "selected": selected, "rejected": rejected})
            save_checkpoint(checkpoint_path, state)
            writeLog("info", logger, "[Selection] Progress saved. Stopping.")
            break
        else:
            p["selected"] = False
            rejected.append(p)

        state.update({"last_index": i + 1, "selected": selected, "rejected": rejected})
        save_checkpoint(checkpoint_path, state)

    return selected, rejected


def save_selected(selected: list, output_dir: Path) -> Path:
    file = output_dir / "selected_papers.json"
    with open(file, "w", encoding="utf-8") as f:
        json.dump(selected, f, indent=2, ensure_ascii=False)
    return file


# --------------------------------------------------
# ENTRY POINT
# --------------------------------------------------
def processEnrichmentEngine():
    input_dir, output_dir = inicioModulo("processEnrichmentEngine")
    ranked_file = output_dir / "ranked_papers.json"

    if not ranked_file.exists():
        writeLog("error", logger, f"Not found: {ranked_file}")
        return None

    with open(ranked_file, "r", encoding="utf-8") as f:
        ranked_papers = json.load(f)

    # Filtrar papers con título o abstract None
    valid_papers = []
    for p in ranked_papers:
        if p.get('title') is None:
            writeLog("warning", logger, f"[Enrichment] Skipping paper with None title: {p.get('paper_id', 'unknown')}")
            continue
        if p.get('abstract') is None:
            writeLog("warning", logger,
                     f"[Enrichment] Skipping paper with None abstract: {p.get('paper_id', 'unknown')} - {p.get('title', '')[:50]}")
            continue
        valid_papers.append(p)

    if len(valid_papers) < len(ranked_papers):
        writeLog("info", logger,
                 f"[Enrichment] Filtered out {len(ranked_papers) - len(valid_papers)} papers with missing title/abstract")

    writeLog("info", logger, f"[Enrichment] Loaded {len(valid_papers)} valid papers from ranked_papers.json")

    # 1. Enrich
    enriched = enrichment_engine(valid_papers)

    # 2. Build review
    review = build_candidate_review(enriched)

    # 3. Generar PDFs para revisión
    original_review, spanish_review = build_dual_review(review)
    generate_review_pdf(original_review, output_dir / "review_original.pdf", top_k=100)
    generate_review_pdf(spanish_review, output_dir / "review_es.pdf", top_k=100)

    # 4. Interactive selection
    selected, rejected = interactive_selection(review, output_dir)

    # 5. Guardar selected_papers.json
    selected_file = save_selected(selected, output_dir)

    writeLog("info", logger, f"[Enrichment] Selected: {len(selected)} papers")
    writeLog("info", logger, f"[Enrichment] Rejected: {len(rejected)} papers")
    writeLog("info", logger, f"[Enrichment] Saved to {selected_file}")
    writeLog("info", logger, "✅ [END] processEnrichmentEngine")

    return {
        "selected": str(selected_file),
        "total_selected": len(selected),
    }


if __name__ == "__main__":
    processEnrichmentEngine()