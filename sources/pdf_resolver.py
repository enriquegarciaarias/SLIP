# pdf_resolver.py
"""
PDF URL resolution cascade for academic papers.

Resolution levels (each attempted only if the previous fails):
  L1 — OpenAlex        open_access.oa_url
  L2 — Unpaywall       best_oa_location.url_for_pdf
  L3 — Semantic Scholar openAccessPdf.url
  L4 — ACL Anthology   deterministic DOI → URL transform (no HTTP)
  L5 — arXiv           title search via arXiv API

No scraping. All sources are public APIs designed for programmatic use.
"""

import re
import time
import requests
from sources.common.common import logger, writeLog


# --------------------------------------------------
# CONFIG
# --------------------------------------------------

UNPAYWALL_EMAIL = "ega3646209@gmail.com"
REQUEST_TIMEOUT = 15
SLEEP_BETWEEN   = 0.3   # polite rate limiting between HTTP calls

# ACL Anthology DOI prefix — PDF URL is deterministic for these
ACL_DOI_PREFIX = "10.18653/"


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def _get(url: str) -> dict | None:
    """Safe GET — returns parsed JSON or None on any failure."""
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT,
                         headers={"User-Agent": "SLIP/1.0 (research pipeline)"})
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _is_pdf_url(url: str | None) -> bool:
    """
    Basic sanity check: the URL must exist and point to something
    plausibly downloadable (ends in .pdf or contains /pdf/).
    Prevents storing landing-page URLs as pdf_url.
    """
    if not url:
        return False
    url_lower = url.lower()
    return url_lower.endswith(".pdf") or "/pdf/" in url_lower


# --------------------------------------------------
# L1 — OpenAlex
# --------------------------------------------------

def resolve_openalex(doi: str) -> str | None:
    """
    Queries OpenAlex Works API by DOI.
    Returns the best OA PDF URL from open_access.oa_url if it looks
    like a direct PDF link; otherwise returns the oa_url as a landing
    page fallback (still more useful than nothing for manual retrieval).
    """
    if not doi:
        return None

    url  = f"https://api.openalex.org/works/https://doi.org/{doi}"
    data = _get(url)
    if not data:
        return None

    oa = data.get("open_access", {})

    # Prefer a direct PDF link
    pdf_url = oa.get("oa_url")
    if pdf_url:
        return pdf_url

    return None


# --------------------------------------------------
# L2 — Unpaywall
# --------------------------------------------------

def resolve_unpaywall(doi: str) -> str | None:
    """
    Queries Unpaywall API by DOI.
    Returns pdf_url from best_oa_location only — ignores landing pages.
    """
    if not doi:
        return None

    url  = f"https://api.unpaywall.org/v2/{doi}?email={UNPAYWALL_EMAIL}"
    data = _get(url)
    if not data:
        return None

    best = data.get("best_oa_location") or {}
    return best.get("url_for_pdf")  # None if not OA or no PDF


# --------------------------------------------------
# L3 — Semantic Scholar
# --------------------------------------------------

def resolve_semantic_scholar(doi: str, title: str) -> str | None:
    """
    Queries Semantic Scholar Graph API.
    Tries DOI lookup first; falls back to title search.
    Returns openAccessPdf.url if available.
    S2 has excellent coverage of NLP/AI proceedings (ACL, EMNLP, arXiv).
    """
    fields = "openAccessPdf,externalIds"

    # Try by DOI
    if doi:
        url  = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields={fields}"
        data = _get(url)
        if data:
            pdf = data.get("openAccessPdf") or {}
            pdf_url = pdf.get("url")
            if pdf_url:
                return pdf_url

    # Fallback: title search (top-1 result)
    if title:
        time.sleep(SLEEP_BETWEEN)
        query = requests.utils.quote(title[:150])
        url   = (f"https://api.semanticscholar.org/graph/v1/paper/search"
                 f"?query={query}&fields={fields}&limit=1")
        data  = _get(url)
        if data:
            papers = data.get("data", [])
            if papers:
                pdf = papers[0].get("openAccessPdf") or {}
                return pdf.get("url")

    return None


# --------------------------------------------------
# L4 — ACL Anthology (deterministic, no HTTP)
# --------------------------------------------------

def resolve_acl_anthology(doi: str) -> str | None:
    """
    For papers with DOI prefix 10.18653/ (ACL/EMNLP/NAACL/EACL/COLING),
    the PDF URL is fully determined by the DOI — no HTTP request needed.

    Transform:
        doi:10.18653/v1/2023.findings-acl.820
        → https://aclanthology.org/2023.findings-acl.820.pdf

    This covers the majority of NLP/AI papers in a typical SLIP corpus.
    """
    if not doi:
        return None

    # Normalise: strip leading "doi:" or "https://doi.org/"
    clean = re.sub(r'^(doi:|https?://doi\.org/)', '', doi.strip())

    if not clean.startswith(ACL_DOI_PREFIX):
        return None

    # Extract the ACL ID: everything after "10.18653/v1/"
    # e.g. "2023.findings-acl.820"
    match = re.match(r'10\.18653/v1/(.+)', clean)
    if not match:
        return None

    acl_id = match.group(1)
    return f"https://aclanthology.org/{acl_id}.pdf"


# --------------------------------------------------
# L5 — arXiv
# --------------------------------------------------

def resolve_arxiv(title: str) -> str | None:
    """
    Searches arXiv API by title (ti: query).
    Returns the PDF URL for the top result if the title match is close.

    Note: arXiv search is fuzzy — we do a basic title overlap check
    (≥60% of title words must appear in the result title) to avoid
    false positives from short or common titles.
    """
    if not title:
        return None

    query = requests.utils.quote(f'ti:"{title[:100]}"')
    url   = (f"https://export.arxiv.org/api/query"
             f"?search_query={query}&max_results=1")

    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT,
                         headers={"User-Agent": "SLIP/1.0 (research pipeline)"})
        if r.status_code != 200:
            return None
    except Exception:
        return None

    # arXiv API returns Atom XML — parse minimally
    content = r.text

    # Extract arxiv id from <id>http://arxiv.org/abs/XXXX.XXXXX</id>
    id_match = re.search(r'<id>http://arxiv\.org/abs/([^<]+)</id>', content)
    if not id_match:
        return None

    # Extract result title for overlap check
    title_match = re.search(r'<title>([^<]+)</title>', content)
    if title_match:
        result_title = title_match.group(1).lower()
        query_words  = set(title.lower().split())
        result_words = set(result_title.split())
        overlap      = len(query_words & result_words) / max(len(query_words), 1)
        if overlap < 0.6:
            return None   # likely a false positive

    arxiv_id = id_match.group(1).strip()
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf"


# --------------------------------------------------
# PUBLIC INTERFACE
# --------------------------------------------------

def resolve_pdf_url(doi: str, title: str) -> dict:
    """
    Runs the full resolution cascade L1 → L5.
    Returns a dict with the resolved URL and the level that found it,
    or pdf_url=None with level="unresolved" if all levels fail.

    Usage:
        result = resolve_pdf_url(doi="10.18653/v1/2023.findings-acl.820",
                                 title="Selecting Better Samples...")
        result["pdf_url"]   # str | None
        result["pdf_source"] # "openalex" | "unpaywall" | "semantic_scholar"
                              # | "acl_anthology" | "arxiv" | "unresolved"
    """

    # L4 first — deterministic, free, no HTTP — check before any network call
    pdf_url = resolve_acl_anthology(doi)
    if pdf_url:
        writeLog("info", logger,
                 f"[PDFResolver] L4-ACL  {doi or title[:50]!r}")
        return {"pdf_url": pdf_url, "pdf_source": "acl_anthology"}

    # L1 — OpenAlex
    time.sleep(SLEEP_BETWEEN)
    pdf_url = resolve_openalex(doi)
    if pdf_url:
        writeLog("info", logger,
                 f"[PDFResolver] L1-OA   {doi or title[:50]!r}")
        return {"pdf_url": pdf_url, "pdf_source": "openalex"}

    # L2 — Unpaywall
    time.sleep(SLEEP_BETWEEN)
    pdf_url = resolve_unpaywall(doi)
    if pdf_url:
        writeLog("info", logger,
                 f"[PDFResolver] L2-UP   {doi or title[:50]!r}")
        return {"pdf_url": pdf_url, "pdf_source": "unpaywall"}

    # L3 — Semantic Scholar
    time.sleep(SLEEP_BETWEEN)
    pdf_url = resolve_semantic_scholar(doi, title)
    if pdf_url:
        writeLog("info", logger,
                 f"[PDFResolver] L3-S2   {doi or title[:50]!r}")
        return {"pdf_url": pdf_url, "pdf_source": "semantic_scholar"}

    # L5 — arXiv
    time.sleep(SLEEP_BETWEEN)
    pdf_url = resolve_arxiv(title)
    if pdf_url:
        writeLog("info", logger,
                 f"[PDFResolver] L5-arXiv {doi or title[:50]!r}")
        return {"pdf_url": pdf_url, "pdf_source": "arxiv"}

    writeLog("info", logger,
             f"[PDFResolver] unresolved {doi or title[:50]!r}")
    return {"pdf_url": None, "pdf_source": "unresolved"}