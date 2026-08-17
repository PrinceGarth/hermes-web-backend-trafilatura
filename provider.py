"""Local HTML and PDF extraction for Hermes Agent.

The provider uses Hermes' own URL-policy and SSRF-safe HTTP client, then
dispatches static HTML to Trafilatura and text PDFs to pypdf.  It deliberately
does not install packages at runtime: operators can pin and audit dependencies
before enabling the plugin, and ``security.allow_lazy_installs: false`` remains
an effective boundary.
"""

from __future__ import annotations

import asyncio
import html as html_module
import io
import logging
import re
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 30
_MIN_CONTENT_CHARS = 100
_MAX_BODY_BYTES = 15 * 1024 * 1024
_MAX_PDF_PAGES = 200
_MAX_EXTRACTED_CHARS = 2_000_000
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

def _dependency_error(package: str) -> str:
    return (
        f"{package} is not installed. Install the plugin's audited dependencies "
        "into the Hermes venv: uv pip install --python <hermes-venv-python> "
        "'trafilatura>=2,<3' 'pypdf>=6,<7'"
    )


async def _ssrf_redirect_guard(response):
    """Re-validate each redirect target — a public URL can 302 to
    http://169.254.169.254/ and bypass the pre-fetch check otherwise."""
    from tools.url_safety import async_is_safe_url, redirect_target_from_response

    redirect_url = redirect_target_from_response(response)
    if redirect_url and not await async_is_safe_url(redirect_url):
        raise ValueError(f"Blocked redirect to private/internal address: {redirect_url}")
    if redirect_url:
        blocked = check_website_access(redirect_url)
        if blocked:
            raise ValueError(blocked["message"])


def _extract_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return html_module.unescape(re.sub(r"\s+", " ", match.group(1))).strip()


def _content_type(response: Any) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _looks_like_pdf(content_type: str, body: bytes) -> bool:
    return content_type == "application/pdf" or body.lstrip().startswith(b"%PDF-")


def _extract_pdf(body: bytes, url: str) -> Dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"url": url, "title": "", "content": "", "error": _dependency_error("pypdf")}

    try:
        reader = PdfReader(io.BytesIO(body), strict=False)
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:  # noqa: BLE001 - encrypted documents vary by producer
                unlocked = 0
            if not unlocked:
                return {
                    "url": url,
                    "title": "",
                    "content": "",
                    "error": "PDF is encrypted and cannot be extracted without a password",
                }

        pages = reader.pages
        page_count = len(pages)
        parts: List[str] = []
        extracted_chars = 0
        pages_extracted = 0
        for page in pages:
            if pages_extracted >= _MAX_PDF_PAGES:
                break
            text = page.extract_text() or ""
            pages_extracted += 1
            if text.strip():
                parts.append(text.strip())
                extracted_chars += len(parts[-1])
            if extracted_chars >= _MAX_EXTRACTED_CHARS:
                break

        extracted = "\n\n".join(parts)[:_MAX_EXTRACTED_CHARS].strip()
        if not extracted:
            return {
                "url": url,
                "title": "",
                "content": "",
                "error": "PDF contains no extractable text (it may be scanned or image-only)",
            }

        title = ""
        author = ""
        metadata = reader.metadata
        if metadata:
            title = str(metadata.title or "").strip()
            author = str(metadata.author or "").strip()
        markdown = f"# {title}\n\n{extracted}" if title else extracted
        return {
            "url": url,
            "title": title,
            "content": markdown,
            "raw_content": markdown,
            "metadata": {
                "sourceURL": url,
                "engine": "pypdf",
                "author": author,
                "pages": page_count,
                "pages_extracted": pages_extracted,
            },
        }
    except Exception as exc:  # noqa: BLE001 - malformed PDFs are per-URL failures
        return {
            "url": url,
            "title": "",
            "content": "",
            "error": f"PDF extraction failed: {exc}",
        }


class TrafilaturaExtractProvider(WebSearchProvider):
    """Local content extraction via trafilatura. Extract-only, no search."""

    @property
    def name(self) -> str:
        return "trafilatura"

    @property
    def display_name(self) -> str:
        return "Trafilatura (trafilatura)"

    def is_available(self) -> bool:
        try:
            import trafilatura  # noqa: F401

            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return False

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "free · no key · self-hosted · extract only",
            "tag": "Static HTML + text-PDF extraction. No API key or cloud reader.",
            "env_vars": [],
        }

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        from tools.url_safety import create_ssrf_safe_async_client

        try:
            import trafilatura
        except ImportError:
            error = _dependency_error("trafilatura")
            return [{"url": url, "title": "", "content": "", "error": error} for url in urls]

        results: List[Dict[str, Any]] = []

        for url in urls:
            blocked = check_website_access(url)
            if blocked:
                results.append(
                    {"url": url, "title": "", "content": "", "error": blocked["message"]}
                )
                continue

            try:
                async with create_ssrf_safe_async_client(
                    timeout=_FETCH_TIMEOUT,
                    follow_redirects=True,
                    event_hooks={"response": [_ssrf_redirect_guard]},
                ) as client:
                    async with client.stream(
                        "GET",
                        url,
                        headers={
                            "User-Agent": _USER_AGENT,
                            "Accept": (
                                "text/html,application/xhtml+xml,application/pdf,"
                                "text/plain,*/*;q=0.8"
                            ),
                        },
                    ) as response:
                        response.raise_for_status()
                        declared_size = int(response.headers.get("content-length", "0") or 0)
                        if declared_size > _MAX_BODY_BYTES:
                            raise ValueError("Response body exceeds the 15 MiB extraction limit")
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > _MAX_BODY_BYTES:
                                raise ValueError("Response body exceeds the 15 MiB extraction limit")
                        body = bytes(chunks)
                        final_url = str(response.url)
                        content_type = _content_type(response)

                final_blocked = check_website_access(final_url)
                if final_blocked:
                    results.append(
                        {
                            "url": final_url,
                            "title": "",
                            "content": "",
                            "error": final_blocked["message"],
                        }
                    )
                    continue

                if _looks_like_pdf(content_type, body):
                    results.append(await asyncio.to_thread(_extract_pdf, body, final_url))
                    continue

                encoding = response.encoding or "utf-8"
                text = body.decode(encoding, errors="replace")
                if content_type == "text/plain":
                    extracted = text.strip()
                else:
                    extracted = trafilatura.extract(
                        text,
                        include_comments=False,
                        include_tables=True,
                        include_links=True,
                        output_format="markdown",
                        favor_recall=True,
                    )

                if not extracted or len(extracted.strip()) < _MIN_CONTENT_CHARS:
                    results.append(
                        {
                            "url": final_url,
                            "title": "",
                            "content": "",
                            "error": (
                                "No extractable content (page may be JS-rendered, blocked, "
                                "paywalled, sparse, or empty)"
                            ),
                        }
                    )
                    continue

                results.append(
                    {
                        "url": final_url,
                        "title": _extract_title(text),
                        "content": extracted[:_MAX_EXTRACTED_CHARS],
                        "raw_content": extracted[:_MAX_EXTRACTED_CHARS],
                        "metadata": {"sourceURL": final_url, "engine": "trafilatura"},
                    }
                )

            except Exception as exc:  # noqa: BLE001 — per-URL isolation
                logger.warning("Trafilatura extract failed for %s: %s", url, exc)
                results.append(
                    {"url": url, "title": "", "content": "", "error": f"Extraction failed: {exc}"}
                )

        return results
