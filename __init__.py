"""Trafilatura — local, free web_extract backend. No API key.

Config::

    web:
      extract_backend: trafilatura
"""

from __future__ import annotations

from .provider import TrafilaturaExtractProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(TrafilaturaExtractProvider())
