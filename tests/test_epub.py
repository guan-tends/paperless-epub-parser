"""Tests for the namespace-tolerant container/OPF reader.

These build EPUB zips on the fly rather than depending on external fixtures,
so they are hermetic and can express shapes that a real library may not contain.

The bug this module exists to fix is a *silent* one: markitdown's
``EpubConverter`` looks the spine up with ``getElementsByTagName("itemref")``,
which matches the qualified name and therefore finds nothing in an OPF whose
elements carry a namespace prefix.  The converter then returns only the
metadata block -- a few hundred characters for a several-hundred-thousand
character book -- without raising.  These tests pin both the detection of that
condition and the correctness of the replacement path.
"""

from __future__ import annotations

import zipfile

import pytest

from paperless_epub_parser._epub import EpubStructureError
from paperless_epub_parser._epub import read_epub
from paperless_epub_parser._epub import spine_lookup_is_defeated

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{opf_path}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""


def _opf(prefix: str, chapters: int = 3) -> str:
    """Build an OPF body, optionally namespace-prefixed.

    ``prefix`` of ``""`` produces the ordinary shape; ``"opf:"`` produces the
    shape that defeats markitdown's lookup.  Both are legal.
    """
    p = prefix
    items = "\n".join(
        f'    <{p}item id="ch{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(1, chapters + 1)
    )
    refs = "\n".join(f'    <{p}itemref idref="ch{i}"/>' for i in range(1, chapters + 1))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<{p}package version="2.0" unique-identifier="BookId"
   xmlns:{p.rstrip(':') or 'x'}="http://www.idpf.org/2007/opf">
  <{p}metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Test Book</dc:title>
    <dc:creator>A. Author</dc:creator>
    <dc:language>en</dc:language>
  </{p}metadata>
  <{p}manifest>
{items}
  </{p}manifest>
  <{p}spine toc="ncx">
{refs}
  </{p}spine>
</{p}package>"""


def _write_epub(tmp_path, prefix="", chapters=3, opf_path="OEBPS/content.opf"):
    """Create a minimal but structurally valid EPUB and return its path.

    Content documents are written alongside the OPF, since manifest hrefs
    resolve relative to the OPF's directory -- writing them elsewhere would
    produce an internally inconsistent archive.
    """
    path = tmp_path / "book.epub"
    body = _opf(prefix, chapters)
    base = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER.format(opf_path=opf_path))
        z.writestr(opf_path, body)
        for i in range(1, chapters + 1):
            entry = f"{base}/ch{i}.xhtml" if base else f"ch{i}.xhtml"
            z.writestr(
                entry,
                f'<html><body><h1>Chapter {i}</h1><p>Text of chapter {i}.</p></body></html>',
            )
    return path


class TestReadEpub:
    def test_reads_unprefixed_spine_in_order(self, tmp_path):
        contents = read_epub(_write_epub(tmp_path))
        assert contents.documents == ["OEBPS/ch1.xhtml", "OEBPS/ch2.xhtml", "OEBPS/ch3.xhtml"]

    def test_reads_prefixed_spine_in_order(self, tmp_path):
        """The bug case: prefixed elements must still be found."""
        contents = read_epub(_write_epub(tmp_path, prefix="opf:"))
        assert contents.documents == ["OEBPS/ch1.xhtml", "OEBPS/ch2.xhtml", "OEBPS/ch3.xhtml"]

    def test_extracts_metadata(self, tmp_path):
        contents = read_epub(_write_epub(tmp_path))
        assert contents.title == "Test Book"
        assert contents.metadata["creators" if "creators" in contents.metadata else "authors"] == "A. Author"
        assert contents.metadata["language"] == "en"

    def test_extracts_metadata_from_prefixed_opf(self, tmp_path):
        contents = read_epub(_write_epub(tmp_path, prefix="opf:"))
        assert contents.title == "Test Book"
        assert contents.metadata["language"] == "en"

    def test_reading_order_follows_spine_not_archive_order(self, tmp_path):
        """Spine order is the book's order; ZIP order is incidental."""
        path = tmp_path / "book.epub"
        opf = """<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" unique-identifier="B" xmlns="http://www.idpf.org/2007/opf">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata>
  <manifest>
    <item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>
    <item id="b" href="b.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="b"/><itemref idref="a"/></spine>
</package>"""
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", CONTAINER.format(opf_path="content.opf"))
            z.writestr("content.opf", opf)
            # Deliberately stored in the opposite order to the spine.
            z.writestr("a.xhtml", "<html><body>A</body></html>")
            z.writestr("b.xhtml", "<html><body>B</body></html>")

        assert read_epub(path).documents == ["b.xhtml", "a.xhtml"]

    def test_skips_non_linear_itemrefs(self, tmp_path):
        """linear="no" marks supplementary matter outside the reading order."""
        path = tmp_path / "book.epub"
        opf = """<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" unique-identifier="B" xmlns="http://www.idpf.org/2007/opf">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata>
  <manifest>
    <item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>
    <item id="b" href="b.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="a"/><itemref idref="b" linear="no"/></spine>
</package>"""
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", CONTAINER.format(opf_path="content.opf"))
            z.writestr("content.opf", opf)
            z.writestr("a.xhtml", "<html><body>A</body></html>")
            z.writestr("b.xhtml", "<html><body>B</body></html>")

        assert read_epub(path).documents == ["a.xhtml"]

    def test_resolves_percent_encoded_hrefs(self, tmp_path):
        """Manifest hrefs are URIs; ZIP entry names are not."""
        path = tmp_path / "book.epub"
        opf = """<?xml version="1.0" encoding="UTF-8"?>
<package version="2.0" unique-identifier="B" xmlns="http://www.idpf.org/2007/opf">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata>
  <manifest>
    <item id="a" href="my%20chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="a"/></spine>
</package>"""
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", CONTAINER.format(opf_path="content.opf"))
            z.writestr("content.opf", opf)
            z.writestr("my chapter.xhtml", "<html><body>A</body></html>")

        assert read_epub(path).documents == ["my chapter.xhtml"]

    def test_handles_opf_at_archive_root(self, tmp_path):
        """The OPF is not always nested under a directory."""
        contents = read_epub(_write_epub(tmp_path, opf_path="content.opf"))
        assert contents.documents == ["ch1.xhtml", "ch2.xhtml", "ch3.xhtml"]


class TestReadEpubFailures:
    def test_missing_container_raises(self, tmp_path):
        path = tmp_path / "book.epub"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
        with pytest.raises(EpubStructureError, match="container.xml"):
            read_epub(path)

    def test_not_a_zip_raises(self, tmp_path):
        path = tmp_path / "book.epub"
        path.write_bytes(b"this is not a zip file")
        with pytest.raises(EpubStructureError, match="not a valid ZIP"):
            read_epub(path)

    def test_container_without_rootfile_raises(self, tmp_path):
        path = tmp_path / "book.epub"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml",
                       '<?xml version="1.0"?><container><rootfiles/></container>')
        with pytest.raises(EpubStructureError, match="no rootfile"):
            read_epub(path)

    def test_missing_opf_raises(self, tmp_path):
        path = tmp_path / "book.epub"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", CONTAINER.format(opf_path="missing.opf"))
        with pytest.raises(EpubStructureError, match="OPF not found"):
            read_epub(path)


class TestSpineLookupIsDefeated:
    """The detection must be precise -- false positives would reroute books
    that markitdown handles correctly."""

    def test_false_for_ordinary_opf(self, tmp_path):
        assert spine_lookup_is_defeated(_write_epub(tmp_path)) is False

    def test_true_for_namespace_prefixed_opf(self, tmp_path):
        assert spine_lookup_is_defeated(_write_epub(tmp_path, prefix="opf:")) is True

    def test_true_for_any_other_prefix(self, tmp_path):
        """The defect is about prefixes generally, not the literal 'opf'."""
        assert spine_lookup_is_defeated(_write_epub(tmp_path, prefix="o:")) is True

    def test_returns_false_for_unreadable_file(self, tmp_path):
        """An unreadable file is left for the primary path to report."""
        path = tmp_path / "book.epub"
        path.write_bytes(b"not a zip")
        assert spine_lookup_is_defeated(path) is False

    def test_returns_false_for_missing_file(self, tmp_path):
        assert spine_lookup_is_defeated(tmp_path / "absent.epub") is False

    def test_false_when_opf_has_no_spine_elements(self, tmp_path):
        path = tmp_path / "book.epub"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", CONTAINER.format(opf_path="content.opf"))
            z.writestr("content.opf", '<?xml version="1.0"?><package><manifest/></package>')
        assert spine_lookup_is_defeated(path) is False
