# pdf_resolver.py
"""
PDF Service Module (SOA).
Responsibilities:
  1. Resolve PDF URLs via API cascade (L1-L5).
  2. Robust binary download with magic-byte validation.
"""

import re
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import MaxRetryError
from urllib3.util.retry import Retry
from pathlib import Path
from sources.common.common import logger, writeLog

# --------------------------------------------------
# CONFIG
# --------------------------------------------------
UNPAYWALL_EMAIL = "ega3646209@gmail.com"
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN = 0.3
MIN_PDF_SIZE = 10000
ACL_DOI_PREFIX = "10.18653/"


class _Skip429Retry(Retry):
    """
    Retry que NO reintenta HTTP 429 (rate-limit) y NO respeta la cabecera
    `Retry-After`. Algunas APIs (p. ej. OpenAlex) devuelven 429 con
    `Retry-After` de miles de segundos cuando agotan la cuota; honrarla
    congelaría el pipeline durante horas (se quedaba "colgado").
    """

    def increment(
        self,
        method=None,
        url=None,
        response=None,
        error=None,
        _pool=None,
        _stacktrace=None,
        *args,
        **kwargs,
    ):
        if response is not None and getattr(response, "status", None) == 429:
            raise MaxRetryError(
                _pool, url or "", "Rate limited (HTTP 429) — no retry"
            )
        return super().increment(
            method=method,
            url=url,
            response=response,
            error=error,
            _pool=_pool,
            _stacktrace=_stacktrace,
            *args,
            **kwargs,
        )


# --------------------------------------------------
# INFRASTRUCTURE: Robust HTTP Client (Reutilizable)
# --------------------------------------------------
def _get_robust_session() -> requests.Session:
    """Crea una sesión con reintentos y cabeceras realistas contra bloqueos."""
    session = requests.Session()
    retry = _Skip429Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        respect_retry_after_header=False,
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
    })
    return session


# Cache de sesión a nivel de módulo para reutilizar conexiones
_session = None


def get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = _get_robust_session()
    return _session


# --------------------------------------------------
# FEATURE 1: BINARY DOWNLOAD & VALIDATION
# --------------------------------------------------
def download_pdf_file(url: str, output_file: Path) -> bool:
    """
    Descarga un PDF de forma robusta.
    Valida 'Magic Bytes' (%PDF-) en lugar de Content-Type.
    """
    if not url:
        return False

    try:
        session = get_session()
        headers = {"Referer": url}  # Evita bloqueos por Hotlinking
        r = session.get(url, timeout=60, stream=True, allow_redirects=True, headers=headers)

        with open(output_file, "wb") as f:
            for i, chunk in enumerate(r.iter_content(8192)):
                if chunk:
                    if i == 0 and not chunk.startswith(b'%PDF-'):
                        output_file.unlink(missing_ok=True)
                        return False
                    f.write(chunk)

        if output_file.stat().st_size < MIN_PDF_SIZE:
            writeLog("warning", logger, f"PDF too small (<{MIN_PDF_SIZE} bytes), discarding: {output_file.name}")
            output_file.unlink(missing_ok=True)
            return False

        return True
    except Exception as e:
        output_file.unlink(missing_ok=True)
        writeLog("debug", logger, f"Download failed for {url}: {str(e)}")
        return False


# --------------------------------------------------
# FEATURE 2: URL RESOLUTION CASCADE (L1 -> L5)
# --------------------------------------------------
def _get(url: str) -> dict | None:
    """Safe GET usando la sesión robusta."""
    try:
        r = get_session().get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def resolve_openalex(doi: str) -> str | None:
    if not doi: return None
    data = _get(f"https://api.openalex.org/works/https://doi.org/{doi}")
    return data.get("open_access", {}).get("oa_url") if data else None


def resolve_unpaywall(doi: str) -> str | None:
    if not doi: return None
    data = _get(f"https://api.unpaywall.org/v2/{doi}?email={UNPAYWALL_EMAIL}")
    return (data.get("best_oa_location") or {}).get("url_for_pdf") if data else None


def resolve_semantic_scholar(doi: str, title: str) -> str | None:
    fields = "openAccessPdf,externalIds"
    if doi:
        data = _get(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields={fields}")
        if data: return (data.get("openAccessPdf") or {}).get("url")
    if title:
        time.sleep(SLEEP_BETWEEN)
        query = requests.utils.quote(title[:150])
        data = _get(f"https://api.semanticscholar.org/graph/v1/paper/search?query={query}&fields={fields}&limit=1")
        results = (data or {}).get("data") or []
        if results: return (results[0].get("openAccessPdf") or {}).get("url")
    return None


def resolve_acl_anthology(doi: str) -> str | None:
    if not doi: return None
    clean = re.sub(r'^(doi:|https?://doi\.org/)', '', doi.strip())
    match = re.match(r'10\.18653/v1/(.+)', clean)
    return f"https://aclanthology.org/{match.group(1)}.pdf" if match else None


def resolve_arxiv(title: str) -> str | None:
    if not title: return None
    try:
        r = requests.get(
            f"https://export.arxiv.org/api/query?search_query={requests.utils.quote(f'ti:\"{title[:100]}\"')}&max_results=1",
            timeout=REQUEST_TIMEOUT)
        if r.status_code != 200: return None
        id_match = re.search(r'<id>http://arxiv\.org/abs/([^<]+)</id>', r.text)
        if not id_match: return None
        title_match = re.search(r'<title>([^<]+)</title>', r.text)
        if title_match:
            overlap = len(set(title.lower().split()) & set(title_match.group(1).lower().split())) / max(
                len(title.split()), 1)
            if overlap < 0.6: return None
        return f"https://arxiv.org/pdf/{id_match.group(1).strip()}.pdf"
    except Exception:
        return None


# --------------------------------------------------
# PUBLIC INTERFACES (API del Servicio)
# --------------------------------------------------
def resolve_pdf_url(doi: str, title: str) -> dict:
    """Resuelve la URL sin descargar."""
    # L4, L1, L2, L3, L5
    cascade = [
        ("acl_anthology", lambda: resolve_acl_anthology(doi)),
        ("openalex", lambda: resolve_openalex(doi)),
        ("unpaywall", lambda: resolve_unpaywall(doi)),
        ("semantic_scholar", lambda: resolve_semantic_scholar(doi, title)),
        ("arxiv", lambda: resolve_arxiv(title))
    ]

    for source_name, resolver in cascade:
        time.sleep(SLEEP_BETWEEN)
        pdf_url = resolver()
        if pdf_url:
            writeLog("info", logger, f"[PDFResolver] Resolved via {source_name}: {pdf_url}")
            return {"pdf_url": pdf_url, "pdf_source": source_name}

    return {"pdf_url": None, "pdf_source": "unresolved"}


def resolve_and_download(paper: dict, output_file: Path) -> bool:
    """
    Orquestador: Intenta descargar la URL existente, si falla
    lanza la cascada de resolución e intenta con la nueva URL.
    """
    existing_url = paper.get("pdf_url")

    # 1. Intentar con la URL que ya tenga (del enrichment)
    if existing_url and existing_url != "None":
        if download_pdf_file(existing_url, output_file):
            return True

    # 2. Si falló o no existía, usar la cascada completa
    writeLog("info", logger, f"Direct URL failed/missing. Triggering resolution cascade...")
    resolution = resolve_pdf_url(paper.get("doi", ""), paper.get("title", ""))
    new_url = resolution.get("pdf_url")

    if new_url:
        # Actualizamos el paper por si acaso el orquestador quiere guardar el nuevo estado
        paper["pdf_url"] = new_url
        paper["pdf_source"] = resolution["pdf_source"]
        return download_pdf_file(new_url, output_file)

    return False