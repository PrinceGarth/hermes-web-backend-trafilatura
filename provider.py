"""Trafilatura extract provider — local, free, no API key.

Fetches a URL with an SSRF-safe httpx client, extracts main content with
trafilatura, returns markdown. No search, no fallback chain, no third
party — just static HTML in, clean text out.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 30
_MIN_CONTENT_CHARS = 100
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_self_install_attempted = False


def _ensure_trafilatura():
    """Import trafilatura, self-installing once into Hermes' own venv if missing.

    Third-party plugins have no hook into ``hermes tools``' install wizard
    (that dispatch is a hardcoded if/elif on core provider names) — this
    mirrors core's own lazy-install pattern (``tools/lazy_deps.py``) instead
    of leaving the user to run a manual pip command.

    Deliberately NOT done in :meth:`is_available`, which must stay a cheap,
    network-free check (it runs on every ``hermes tools`` paint).
    """
    global _self_install_attempted
    try:
        import trafilatura

        return trafilatura
    except ImportError:
        pass

    if _self_install_attempted:
        raise ImportError(
            "trafilatura is not installed and a self-install attempt already "
            "failed this run. Install manually: "
            "uv pip install --python <hermes venv python> trafilatura"
        )
    _self_install_attempted = True

    import shutil
    import subprocess
    import sys

    logger.info("trafilatura not found — attempting self-install into Hermes' venv")

    uv = shutil.which("uv")
    cmd = (
        [uv, "pip", "install", "--python", sys.executable, "trafilatura"]
        if uv
        else [sys.executable, "-m", "pip", "install", "trafilatura"]
    )
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0 and not uv:
        # uv-created venvs ship without pip — bootstrap it, then retry once.
        subprocess.run(
            [sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        logger.warning("trafilatura self-install failed: %s", result.stderr.strip()[-500:])
        raise ImportError(
            f"trafilatura self-install failed (exit {result.returncode}). "
            f"Install manually: uv pip install --python {sys.executable} trafilatura"
        )

    logger.info("trafilatura installed successfully")
    import trafilatura

    return trafilatura


async def _ssrf_redirect_guard(response):
    """Re-validate each redirect target — a public URL can 302 to
    http://169.254.169.254/ and bypass the pre-fetch check otherwise."""
    from tools.url_safety import async_is_safe_url, redirect_target_from_response

    redirect_url = redirect_target_from_response(response)
    if redirect_url and not await async_is_safe_url(redirect_url):
        raise ValueError(f"Blocked redirect to private/internal address: {redirect_url}")


def _extract_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


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
            "tag": "Static-HTML extraction, self-installs on first use. No API key, no cloud call.",
            "env_vars": [],
        }

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        from tools.url_safety import create_ssrf_safe_async_client

        try:
            trafilatura = _ensure_trafilatura()
        except ImportError as exc:
            return [{"url": url, "title": "", "content": "", "error": str(exc)} for url in urls]

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
                    response = await client.get(
                        url,
                        headers={
                            "User-Agent": _USER_AGENT,
                            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                        },
                    )
                    response.raise_for_status()
                    html = response.text
                    final_url = str(response.url)

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

                extracted = trafilatura.extract(
                    html,
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
                            "error": "No extractable content (page may be JS-rendered, "
                            "paywalled, or empty)",
                        }
                    )
                    continue

                results.append(
                    {
                        "url": final_url,
                        "title": _extract_title(html),
                        "content": extracted,
                        "raw_content": extracted,
                        "metadata": {"sourceURL": final_url, "engine": "trafilatura"},
                    }
                )

            except Exception as exc:  # noqa: BLE001 — per-URL isolation
                logger.warning("Trafilatura extract failed for %s: %s", url, exc)
                results.append(
                    {"url": url, "title": "", "content": "", "error": f"Extraction failed: {exc}"}
                )

        return results
