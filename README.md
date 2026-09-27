# paperless-epub-parser

An **EPUB parser plugin for [Paperless-ngx](https://docs.paperless-ngx.com/)** —
makes `.epub` files consumable and full-text searchable.

No fork of Paperless-ngx. The upstream project exposes a supported parser
plugin mechanism: any Python package that advertises a class under the
`paperless_ngx.parsers` entry-point group is discovered automatically at
startup and competes with the built-in parsers on score. This package is that,
and nothing more.

## Why

Paperless-ngx has no built-in EPUB support. Neither Apache Tika (whose Paperless
mime list is hardcoded to Office formats) nor Gotenberg/LibreOffice (which has an
EPUB *export* filter but no *import* filter) can open one. This plugin supplies
the missing parser, so an EPUB library becomes searchable alongside everything
else in the archive.

Scope is deliberately narrow: **text for search**. No archive PDF, no cover-art
thumbnail, no page count. See "Design decisions" below.

## Install

The container's `site-packages` is root-owned and the image is ephemeral, so a
hand-run `pip install` inside a running container does not persist. Build a thin
derived image instead:

```dockerfile
FROM ghcr.io/paperless-ngx/paperless-ngx:3.1.3

USER root
RUN pip install --no-cache-dir /path/to/paperless-epub-parser
USER paperless
```

Then point the `webserver` service at it:

```yaml
services:
  webserver:
    build: .
    # ... rest of your existing service definition
```

⚠️ **Pin the base image tag.** The upstream `latest` tag moves; a rebuild months
later would silently install the plugin into a different Paperless version than
the one you tested against.

Verify discovery in the startup log:

```
Loaded third-party parser 'EPUB' v1.0.0 by David Newman (entrypoint: 'epub').
```

## Design decisions

**Text extraction is delegated to [`markitdown`](https://github.com/microsoft/markitdown).**
Its `EpubConverter` already implements the genuinely fiddly parts: locating the
OPF package document via `META-INF/container.xml`, resolving manifest hrefs
(including percent-encoded ones), walking the spine in *reading order* rather
than filename order, and converting each XHTML chapter to Markdown with headings
and tables preserved. Reimplementing that would duplicate a maintained upstream.

EPUB conversion lives in markitdown's **base** install — the converter subclasses
`HtmlConverter` and needs only `beautifulsoup4`, which is a core dependency.
There is no `[epub]` extra and none is required, so this plugin depends on bare
`markitdown` and deliberately does **not** pull in the PDF/DOCX/PPTX/XLSX extras
(Paperless already handles those formats natively).

**No archive PDF.** EPUB is the readable artifact; it belongs in an e-reader, not
in a PDF viewer. `can_produce_archive` and `requires_pdf_rendition` are both
`False`, so the consumer skips PDF generation entirely.

**The thumbnail is rendered text, not cover art.** This follows the precedent set
by Paperless-ngx's own `TextDocumentParser`. Cover extraction would mean adding
`ebooklib` plus, for the common case of an SVG or fixed-layout cover, an SVG
rasteriser — two dependencies and a whole class of failure (missing, oversized,
malformed, DRM-wrapped covers) in exchange for a nicer grid tile.

**`get_page_count()` returns `None`.** EPUB is a reflowable format. It has no
pages. Any integer would be an invention.

**`get_date()` returns `None`.** EPUB `dc:date` is unreliable in practice — often
a build timestamp rather than a publication date, and absent altogether from the
fixtures this plugin was validated against. Paperless's own date parser reads the
filename and extracted text and does better than a blind metadata grab.

**Fully local.** The parser declares `uses_remote_service = False` and never
contacts anything off-box.

## Failure behaviour

- **Malformed / unreadable file** → `ParseError` naming the file, so Paperless
  reports a document error rather than an "unexpected error".
- **DRM-protected or fixed-layout EPUB** → no extractable flow text, which is
  raised as a `ParseError` with a message that says so. It is not silently filed
  as an empty document.
- **Thumbnail generation never raises.** The consumer calls it unguarded, so a
  failure there would abort an otherwise successful consumption.

## Development

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -v
```

The suite stubs the conversion seam (`_extract_epub`), so it runs without
`markitdown`, Paperless-ngx, or Django installed. It covers the registry
contract, the protocol surface, the failure paths, and the thumbnail guarantee.

Real conversion is verified separately against genuine EPUBs — see below.

## Verification

Unit tests prove the contract. They do not prove that `markitdown` extracts
useful text from a real book. To check that, and to confirm end-to-end discovery
by Paperless:

```bash
# 1. Extraction, standalone
uv venv /tmp/v && uv pip install --python /tmp/v/bin/python markitdown
/tmp/v/bin/python -c "
from markitdown import MarkItDown
r = MarkItDown().convert('book.epub')
print(len(r.text_content or r.markdown or ''))
"

# 2. Discovery, via the container's startup log
docker compose logs webserver | grep 'Loaded third-party parser'

# 3. End-to-end: drop an EPUB into consume/ and watch it become searchable
cp book.epub /path/to/paperless/consume/
```

## Compatibility

- Paperless-ngx **3.1.3** (the parser registry landed in the 3.x series; this
  plugin targets the `ParserProtocol` interface as it exists in 3.1.3).
- Python **3.11+** (the image ships 3.14; `markitdown` supports 3.10–3.14).

## Licence

MIT — David Newman <david.r.newman@proton.me>
