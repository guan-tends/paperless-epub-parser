"""Namespace-tolerant EPUB container/OPF parsing.

Why this module exists
----------------------
``markitdown``'s ``EpubConverter`` handles the overwhelming majority of EPUBs
correctly, and we delegate to it by default.  It has one specific defect,
however, which we correct here.

It locates the spine with ``getElementsByTagName("itemref")`` and the manifest
with ``getElementsByTagName("item")``.  On an OPF whose elements carry a
namespace prefix -- ``<opf:itemref>``, ``<opf:item>`` -- those calls return
**zero** elements, because ``getElementsByTagName`` matches on the *qualified*
name.  The converter then finds no spine, emits only the metadata block, and
returns a few hundred characters of text for a book of several hundred
thousand.  Nothing raises; the document is filed as a near-empty searchable
record.

That is a silent, total extraction failure on a legal and long-standing EPUB 2
shape (namespace-prefixed OPF is what several publisher toolchains emit).  In a
795-book library it affected exactly one book -- which is precisely why it
matters: a hole that size is never noticed by reading output.

The fix is to match on the element's *local* name, stripping any prefix, which
is correct whether or not the document is namespace-aware:

    <itemref>       -> tagName "itemref"      -> local name "itemref"
    <opf:itemref>   -> tagName "opf:itemref"  -> local name "itemref"

Scope
-----
This module only handles the **spine walk** -- locating the OPF, reading
metadata, and yielding content documents in reading order.  Conversion of each
XHTML document to Markdown is still done by ``markitdown``'s ``HtmlConverter``,
so we reuse upstream's HTML handling rather than reimplementing it.  We replace
only the part that is broken.
"""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from urllib.parse import unquote

# Element local names used by the OPF package document.
_CONTAINER_PATH = "META-INF/container.xml"

# Metadata is addressed by local name; the traditional Dublin Core prefix is
# ``dc:`` but some producers omit it, so both shapes are accepted.
_METADATA_FIELDS = (
    ("title", "title"),
    ("creator", "authors"),
    ("language", "language"),
    ("publisher", "publisher"),
    ("date", "date"),
    ("description", "description"),
    ("identifier", "identifier"),
)


class EpubStructureError(Exception):
    """Raised when an EPUB container cannot be read."""


@dataclass
class EpubContents:
    """An EPUB unpacked into a reading-order list of XHTML documents."""

    title: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    # ZIP entry names of the spine documents, in reading order.
    documents: list[str] = field(default_factory=list)


def _parse_xml(payload: bytes):
    """Parse XML with the vendored hardened parser.

    ``defusedxml`` is already a dependency of ``markitdown``, so we borrow it
    rather than adding ``lxml`` -- the same choice upstream makes.  Using a
    defused parser keeps us safe against entity-expansion attacks from
    untrusted EPUBs.
    """
    from defusedxml import minidom

    return minidom.parseString(payload)


def _by_local_name(node, name: str) -> list:
    """Return all descendants whose *local* element name is ``name``.

    ``getElementsByTagName`` matches the qualified name, so it misses
    ``<opf:itemref>`` when asked for ``itemref``.  Comparing the local name
    instead is correct for both prefixed and unprefixed documents.
    """
    return [
        el
        for el in node.getElementsByTagName("*")
        if el.tagName.rsplit(":", 1)[-1] == name
    ]


def _first_text(node, name: str) -> str | None:
    """Return the text of the first local-name match, or ``None``."""
    found = _by_local_name(node, name)
    if not found:
        return None
    # A metadata element may hold its value in a child text node; ``firstChild``
    # can be None for an empty element such as ``<dc:description/>``.
    text = "".join(child.data for child in found[0].childNodes if child.nodeType == child.TEXT_NODE)
    text = text.strip()
    return text or None


def _resolve_href(href: str, base_dir: str, names: set[str]) -> str | None:
    """Resolve an OPF manifest href to a matching ZIP entry name.

    Manifest hrefs are URI references, so reserved characters (spaces, in
    practice) arrive percent-encoded while ZIP entry names are raw.  Try the
    decoded form first, then the literal one, so archives that store either
    shape resolve.  Mirrors the fallback in ``markitdown``'s converter.
    """
    for candidate in (unquote(href), href):
        joined = posixpath.join(base_dir, candidate) if base_dir else candidate
        joined = posixpath.normpath(joined)
        if joined in names:
            return joined
    return None


def _find_opf_path(archive: zipfile.ZipFile) -> str:
    """Locate the OPF package document via ``META-INF/container.xml``."""
    try:
        container = _parse_xml(archive.read(_CONTAINER_PATH))
    except KeyError as exc:
        raise EpubStructureError(f"missing {_CONTAINER_PATH}") from exc

    rootfiles = _by_local_name(container, "rootfile")
    if not rootfiles:
        raise EpubStructureError("container.xml declares no rootfile")

    full_path = rootfiles[0].getAttribute("full-path")
    if not full_path:
        raise EpubStructureError("container.xml rootfile has no full-path")
    return full_path


def read_epub(path: str | Path) -> EpubContents:
    """Unpack an EPUB into metadata plus spine documents in reading order.

    Parameters
    ----------
    path:
        Filesystem path to the EPUB.

    Returns
    -------
    EpubContents
        Metadata and the ZIP entry names of the content documents, ordered as
        the book intends rather than as the archive stores them.

    Raises
    ------
    EpubStructureError
        If the container, OPF, or spine cannot be read.
    """
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise EpubStructureError(f"not a valid ZIP archive: {exc}") from exc

    with archive:
        opf_path = _find_opf_path(archive)
        try:
            opf = _parse_xml(archive.read(opf_path))
        except KeyError as exc:
            raise EpubStructureError(f"OPF not found at {opf_path}") from exc

        metadata: dict[str, str] = {}
        for element_name, key in _METADATA_FIELDS:
            value = _first_text(opf, element_name)
            if value:
                metadata[key] = value
        title = metadata.get("title")

        # Map manifest item id -> href so the spine's idrefs can be resolved.
        manifest: dict[str, str] = {}
        for item in _by_local_name(opf, "item"):
            item_id = item.getAttribute("id")
            href = item.getAttribute("href")
            if item_id and href:
                manifest[item_id] = href

        base_dir = posixpath.dirname(opf_path)
        names = set(archive.namelist())

        documents: list[str] = []
        for itemref in _by_local_name(opf, "itemref"):
            # ``linear="no"`` marks supplementary matter the reading order skips.
            if itemref.getAttribute("linear") == "no":
                continue
            href = manifest.get(itemref.getAttribute("idref"))
            if not href:
                continue
            resolved = _resolve_href(href, base_dir, names)
            if resolved:
                documents.append(resolved)

        return EpubContents(title=title, metadata=metadata, documents=documents)


# ---------------------------------------------------------------------------
# Defect detection and the fallback extraction path
# ---------------------------------------------------------------------------


def spine_lookup_is_defeated(path: str | Path) -> bool:
    """Return ``True`` when ``markitdown``'s spine lookup cannot work on this file.

    This is a *precise* condition, not a heuristic.  ``markitdown`` looks the
    spine up with ``getElementsByTagName("itemref")``, which matches the
    qualified name.  The lookup therefore fails exactly when the OPF's spine
    elements carry a namespace prefix -- i.e. when an element whose local name
    is ``itemref`` has a qualified name that differs from it.

    Detecting the condition itself (rather than guessing from a suspiciously
    short output) means the fallback engages only where it is provably needed,
    with no risk of false positives on books that ``markitdown`` handles fine.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            opf_path = _find_opf_path(archive)
            opf = _parse_xml(archive.read(opf_path))
    except (zipfile.BadZipFile, KeyError, EpubStructureError, OSError):
        # Unreadable here means unreadable for the primary path too; leave the
        # error to be raised (and messaged) there.
        return False

    for element in opf.getElementsByTagName("*"):
        if element.tagName.rsplit(":", 1)[-1] == "itemref":
            return element.tagName != "itemref"
    return False


def extract_text_via_spine(path: str | Path) -> tuple[str, str | None]:
    """Extract Markdown text by walking the spine ourselves.

    Used only for files where :func:`spine_lookup_is_defeated` is true.  Each
    spine document is converted with ``markitdown``'s own ``HtmlConverter``, so
    the HTML-to-Markdown behaviour is identical to the primary path -- we
    replace only the spine walk, which is the part that is broken.

    Returns
    -------
    tuple[str, str | None]
        ``(markdown_text, title)``.
    """
    from markitdown._stream_info import StreamInfo
    from markitdown.converters._html_converter import HtmlConverter

    contents = read_epub(path)
    converter = HtmlConverter()

    parts: list[str] = []
    if contents.metadata:
        header = "\n".join(
            f"**{key.capitalize()}:** {value}"
            for key, value in contents.metadata.items()
            if value
        )
        if header:
            parts.append(header)

    with zipfile.ZipFile(path) as archive:
        available = set(archive.namelist())
        for entry in contents.documents:
            if entry not in available:
                continue
            extension = posixpath.splitext(entry)[1].lower()
            mimetype = {
                ".html": "text/html",
                ".xhtml": "application/xhtml+xml",
                ".htm": "text/html",
            }.get(extension)
            with archive.open(entry) as stream:
                result = converter.convert(
                    stream,
                    StreamInfo(
                        mimetype=mimetype,
                        extension=extension,
                        filename=posixpath.basename(entry),
                    ),
                )
            markdown = getattr(result, "markdown", "") or ""
            if markdown.strip():
                parts.append(markdown.strip())

    return "\n\n".join(parts), contents.title
