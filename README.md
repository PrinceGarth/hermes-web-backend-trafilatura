# hermes-web-backend-trafilatura

Local `web_extract` backend for [Hermes Agent](https://github.com/NousResearch/hermes-agent) using [trafilatura](https://github.com/adbar/trafilatura). Static HTML in, markdown out. No API key, no cloud call, no third-party data path.

Does not render JavaScript — JS-heavy pages will fail extraction (see error message). No search support, extract only.

## Install

```bash
hermes plugins install <your-github>/hermes-web-backend-trafilatura --enable
```

Then set:

```yaml
web:
  extract_backend: trafilatura
```

## Requirements

- `trafilatura` (`pip install trafilatura`)
- `httpx` (already a Hermes dependency)

## How it works

```
URL -> SSRF-safe httpx fetch (redirect-revalidated) -> trafilatura.extract() -> markdown
```

Respects Hermes' website-access blocklist (`security.website_blocklist`) before and after fetch, same as the built-in Firecrawl/Tavily backends.

## License

MIT
