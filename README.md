# hermes-web-backend-trafilatura

Local `web_extract` backend for [Hermes Agent](https://github.com/NousResearch/hermes-agent) using [trafilatura](https://github.com/adbar/trafilatura) for static HTML and [pypdf](https://github.com/py-pdf/pypdf) for text PDFs. HTML/PDF in, markdown out. No API key, cloud reader, or third-party data path.

Does not render JavaScript or OCR scanned PDFs — use Hermes' native browser for JS-heavy pages and an explicit OCR workflow for image-only PDFs. No search support, extract only.

## Install

```bash
hermes plugins install PrinceGarth/hermes-web-backend-trafilatura --enable
```

Then:
- run `hermes tools`
- select "Reconfigure an existing tool's provider or API key"
- go to "Web Search & Scraping"
- Then select the Trafilatura provider

Or manually set in ~/.hermes/config.yaml:

```yaml
web:
  extract_backend: trafilatura
```

## Requirements

- `trafilatura>=2,<3` (GPL-3.0-or-later) for static HTML.
- `pypdf>=6,<7` (BSD-3-Clause) for text PDFs.
- `httpx` (already a Hermes dependency).

Install the two optional dependencies into Hermes' managed venv before enabling the plugin. For a standard venv installation, resolve and verify the interpreter next to the actual Hermes executable:

```bash
HERMES_BIN="$(readlink -f "$(command -v hermes)")"
HERMES_PYTHON="$(dirname "$HERMES_BIN")/python"
test -x "$HERMES_PYTHON"
uv pip install --python "$HERMES_PYTHON" 'trafilatura>=2,<3' 'pypdf>=6,<7'
```

The provider never installs packages at request time, so an operator's `security.allow_lazy_installs: false` boundary remains effective. If your Hermes launcher is not in a standard venv, replace the `--python` value with that launcher's interpreter.

## How it works

```
URL -> SSRF-safe bounded httpx fetch (redirect-revalidated) -> trafilatura or pypdf -> markdown
```

Respects Hermes' website-access blocklist (`security.website_blocklist`) before and after fetch, same as the built-in Firecrawl/Tavily backends.

Responses are capped at 15 MiB before extraction; PDF extraction is capped at 200 pages and output at 2 million characters. Hermes' outer `web_extract` character budget still controls how much reaches the model context.

## License

MIT
