from __future__ import annotations

import io
import sys
import types
import unittest
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import AsyncMock, patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import provider


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        content_type: str,
        url: str = "https://example.com/page",
        *,
        chunks: Optional[List[bytes]] = None,
    ):
        self._body = body
        self.headers = {"content-type": content_type, "content-length": str(len(body))}
        self.url = url
        self.encoding = "utf-8"
        self._chunks = chunks or [body]

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        return None


class FakeStream:
    def __init__(self, response: FakeResponse):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return False


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, *_args, **_kwargs):
        return FakeStream(self.response)


def make_text_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)

    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )

    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Hermes PDF extraction works) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": "Pilot PDF", "/Author": "Hermes"})

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def fake_url_safety(response: FakeResponse):
    module = types.ModuleType("tools.url_safety")
    module.create_ssrf_safe_async_client = lambda **_kwargs: FakeClient(response)
    return patch.dict(sys.modules, {"tools.url_safety": module})


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_title_is_unescaped_and_compacted(self):
        self.assertEqual(provider._extract_title("<title> A &amp;\n B </title>"), "A & B")

    def test_pdf_detection_uses_content_type_or_magic(self):
        self.assertTrue(provider._looks_like_pdf("application/pdf", b"not magic"))
        self.assertTrue(provider._looks_like_pdf("application/octet-stream", b"\n%PDF-1.7"))
        self.assertFalse(provider._looks_like_pdf("text/html", b"<html>"))

    def test_pdf_extraction_returns_markdown_and_metadata(self):
        result = provider._extract_pdf(make_text_pdf(), "https://example.com/pilot.pdf")

        self.assertEqual(result["title"], "Pilot PDF")
        self.assertIn("Hermes PDF extraction works", result["content"])
        self.assertEqual(
            result["metadata"],
            {
                "sourceURL": "https://example.com/pilot.pdf",
                "engine": "pypdf",
                "author": "Hermes",
                "pages": 1,
                "pages_extracted": 1,
            },
        )

    def test_missing_pypdf_returns_actionable_error(self):
        with patch.dict(sys.modules, {"pypdf": None}):
            result = provider._extract_pdf(b"%PDF-1.7", "https://example.com/pilot.pdf")

        self.assertIn("pypdf is not installed", result["error"])

    async def test_redirect_guard_applies_ssrf_and_website_policy(self):
        safe_url = AsyncMock(return_value=True)
        redirect_url = "https://blocked.example/target"
        module = types.ModuleType("tools.url_safety")
        module.async_is_safe_url = safe_url
        module.redirect_target_from_response = lambda _response: redirect_url
        response = SimpleNamespace(is_redirect=True)
        with (
            patch.dict(sys.modules, {"tools.url_safety": module}),
            patch.object(
                provider,
                "check_website_access",
                return_value={"message": "blocked redirect"},
            ),
        ):
            with self.assertRaisesRegex(ValueError, "blocked redirect"):
                await provider._ssrf_redirect_guard(response)

        safe_url.assert_awaited_once_with(redirect_url)

    async def test_provider_extracts_static_html(self):
        body = (
            b"<html><head><title>Capability &amp; Truth</title></head><body><article>"
            b"<h1>Capability truth</h1><p>" + b"Useful owner-visible content. " * 12 + b"</p>"
            b"</article></body></html>"
        )
        response = FakeResponse(body, "text/html")
        with (
            fake_url_safety(response),
            patch.object(provider, "check_website_access", return_value=None),
        ):
            [result] = await provider.TrafilaturaExtractProvider().extract(["https://example.com/page"])

        self.assertEqual(result["title"], "Capability & Truth")
        self.assertIn("Useful owner-visible content", result["content"])
        self.assertEqual(result["metadata"]["engine"], "trafilatura")

    async def test_provider_extracts_pdf(self):
        response = FakeResponse(
            make_text_pdf(), "application/pdf", "https://example.com/pilot.pdf"
        )
        with (
            fake_url_safety(response),
            patch.object(provider, "check_website_access", return_value=None),
        ):
            [result] = await provider.TrafilaturaExtractProvider().extract(
                ["https://example.com/pilot.pdf"]
            )

        self.assertEqual(result["title"], "Pilot PDF")
        self.assertIn("Hermes PDF extraction works", result["content"])
        self.assertEqual(result["metadata"]["engine"], "pypdf")

    async def test_provider_rejects_declared_oversize_response(self):
        response = FakeResponse(b"small", "text/html")
        response.headers["content-length"] = str(provider._MAX_BODY_BYTES + 1)
        with (
            fake_url_safety(response),
            patch.object(provider, "check_website_access", return_value=None),
        ):
            [result] = await provider.TrafilaturaExtractProvider().extract(
                ["https://example.com/huge"]
            )

        self.assertIn("15 MiB extraction limit", result["error"])

    async def test_provider_rejects_streamed_oversize_response(self):
        chunk = b"x" * (provider._MAX_BODY_BYTES // 2 + 1)
        response = FakeResponse(b"", "text/plain", chunks=[chunk, chunk])
        response.headers.pop("content-length")
        with (
            fake_url_safety(response),
            patch.object(provider, "check_website_access", return_value=None),
        ):
            [result] = await provider.TrafilaturaExtractProvider().extract(
                ["https://example.com/streamed-huge"]
            )

        self.assertIn("15 MiB extraction limit", result["error"])

    async def test_provider_preserves_blocklist_failure(self):
        with (
            fake_url_safety(FakeResponse(b"", "text/plain")),
            patch.object(
                provider,
                "check_website_access",
                return_value={"message": "Blocked by website access policy: blocked by policy"},
            ),
        ):
            [result] = await provider.TrafilaturaExtractProvider().extract(
                ["https://example.com/private"]
            )

        self.assertEqual(result["error"], "Blocked by website access policy: blocked by policy")


if __name__ == "__main__":
    unittest.main()
