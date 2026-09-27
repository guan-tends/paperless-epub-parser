"""Tests for the EPUB parser plugin.

These run *without* Paperless-ngx, Django, or ``markitdown`` installed.  The
conversion seam (``_extract_epub``) is patched, which keeps the suite fast and
focused on the contract we actually own: the ``ParserProtocol`` surface, the
registry-facing classmethods, the failure paths, and the thumbnail guarantee.

The real conversion is exercised separately during integration (see README
"Verification"), where a genuine EPUB is pushed through the live stack.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paperless_epub_parser import EpubDocumentParser
from paperless_epub_parser import ParseError
from paperless_epub_parser import _EPUB_MIME_TYPES

EPUB_MIME = "application/epub+zip"
SAMPLE_TEXT = "# Chapter One\n\n" + ("Lorem ipsum dolor sit amet. " * 200)


@pytest.fixture()
def parser():
    """A configured parser with its scratch directory cleaned up after the test."""
    with EpubDocumentParser() as p:
        yield p


def _write_minimal_epub(path, *, mimetype_first: bool = True):
    """Write a structurally valid EPUB and return its path.

    When ``mimetype_first`` is False the archive is written in the shape that
    defeats libmagic: a directory entry first, ``mimetype`` third.  That is a
    real, readable, non-conformant EPUB -- the ``Complete Ninja Collection``
    shape -- and the whole reason the ZIP alias exists.
    """
    import zipfile

    container = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<package version="2.0" xmlns="http://www.idpf.org/2007/opf">'
        "<metadata/><manifest/><spine/></package>"
    )
    with zipfile.ZipFile(path, "w") as z:
        if mimetype_first:
            z.writestr("mimetype", "application/epub+zip")
        else:
            z.writestr("META-INF/", "")
        z.writestr("META-INF/container.xml", container)
        if not mimetype_first:
            z.writestr("mimetype", "application/epub+zip")
        z.writestr("content.opf", opf)
    return path


@pytest.fixture()
def stub_epub(monkeypatch):
    """Patch the conversion seam, returning a controller for its behaviour."""

    class Controller:
        def __init__(self):
            self.text = SAMPLE_TEXT
            self.title = "Stubbed Title"
            self.raises = None
            self.calls = 0

        def __call__(self, path):
            self.calls += 1
            if self.raises is not None:
                raise self.raises
            return self.text, self.title

    controller = Controller()
    monkeypatch.setattr("paperless_epub_parser._extract_epub", controller)
    return controller


class TestRegistryContract:
    """The classmethods Paperless-ngx calls during discovery and dispatch."""

    def test_supported_mime_types_claims_epub(self):
        mimes = EpubDocumentParser.supported_mime_types()
        assert EPUB_MIME in mimes
        assert mimes[EPUB_MIME] == ".epub"

    def test_supported_mime_types_returns_a_copy(self):
        """Callers must not be able to mutate the module-level constant."""
        first = EpubDocumentParser.supported_mime_types()
        first["application/nonsense"] = ".nope"
        assert "application/nonsense" not in EpubDocumentParser.supported_mime_types()

    def test_declared_epub_mime_types_score_full_priority(self):
        """Every EPUB-identifying type scores the conventional 10.

        ``application/zip`` is declared too (see the class docstring on
        :meth:`score`) but is *not* an EPUB-identifying type -- it is scored
        separately by :class:`TestZipAlias`, which is why this iterates the
        explicit set rather than ``supported_mime_types()``.
        """
        for mime in _EPUB_MIME_TYPES:
            assert EpubDocumentParser.score(mime, "book.epub") == 10

    def test_score_declines_unrelated_mime_types(self):
        """Returning None is how a parser declines a file it cannot handle."""
        assert EpubDocumentParser.score("application/pdf", "doc.pdf") is None
        assert EpubDocumentParser.score("text/plain", "notes.txt") is None
        assert EpubDocumentParser.score("", "") is None

    def test_score_accepts_optional_path_argument(self):
        """Paperless always passes ``path=``; the signature must tolerate it."""
        assert EpubDocumentParser.score(EPUB_MIME, "book.epub", Path("/tmp/book.epub")) == 10


class TestZipAlias:
    """Non-conformant EPUBs detected as ``application/zip``.

    libmagic classifies a ZIP archive from its leading bytes alone, and an
    EPUB is a ZIP.  The OCF specification requires the ``mimetype`` entry to be
    the *first* record in the archive, uncompressed; when a producer puts it
    anywhere else, detection reports ``application/zip`` and Paperless rejects
    the file with "Unsupported mime type" *before any parser is consulted*.

    Real instance: ``Complete Ninja Collection`` (Stephen K. Hayes, Black Belt
    Books) -- 37 MB, 277 spine documents, 726,590 characters of perfectly
    extractable text, rejected outright.  See ``test_declared_zip_is_gated_on
    _structure`` and the ``_looks_like_epub`` helper.
    """

    def test_zip_mime_is_declared(self):
        """Claiming the type is *load-bearing*, not cosmetic.

        ``ParserRegistry.get_parser_for_file`` skips any parser whose
        ``supported_mime_types`` omits the detected type -- ``score`` is never
        reached.  A parser cannot inspect a file it is not first offered.
        """
        assert "application/zip" in EpubDocumentParser.supported_mime_types()

    def test_zip_score_is_low_but_nonzero(self, tmp_path):
        """Above zero to be selectable, far below 10 to stay humble.

        A ZIP-shaped EPUB must never out-rank a parser that genuinely handles
        whatever the file turns out to be.
        """
        book = _write_minimal_epub(tmp_path / "book.epub")
        score = EpubDocumentParser.score("application/zip", "book.epub", book)
        assert score is not None
        assert 0 < score < 10

    def test_missing_path_is_declined(self):
        """A path that does not exist cannot be inspected, so decline."""
        assert EpubDocumentParser.score("application/zip", "x.zip", Path("/tmp/does-not-exist.zip")) is None

    def test_real_epub_disguised_as_zip_is_claimed(self, tmp_path):
        """A genuine EPUB detected as a ZIP is accepted."""
        book = _write_minimal_epub(tmp_path / "misdetected.epub")
        assert EpubDocumentParser.score("application/zip", "book.epub", book) == 1

    def test_mimetype_not_first_still_claimed(self, tmp_path):
        """The precise real-world shape, reproduced.

        An archive whose *first* entry is a directory and whose ``mimetype``
        sits third -- still a readable EPUB, still reported as
        ``application/zip`` by libmagic.
        """
        book = _write_minimal_epub(tmp_path / "ninja.epub", mimetype_first=False)
        assert EpubDocumentParser.score("application/zip", "ninja.epub", book) == 1

    def test_ordinary_zip_is_declined(self, tmp_path):
        """A ZIP that is not an EPUB must be declined, not misread as a book."""
        import zipfile

        archive = tmp_path / "holiday-photos.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("photos/beach.jpg", b"\xff\xd8\xff")
        assert EpubDocumentParser.score("application/zip", "holiday-photos.zip", archive) is None

    def test_zip_without_mimetype_is_declined(self, tmp_path):
        """``container.xml`` alone is not enough -- both markers are required."""
        import zipfile

        archive = tmp_path / "not-a-book.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("META-INF/container.xml", "<container/>")
            z.writestr("content.opf", "<package/>")
        assert EpubDocumentParser.score("application/zip", "not-a-book.zip", archive) is None

    def test_zip_with_wrong_mimetype_string_is_declined(self, tmp_path):
        """A ``mimetype`` entry claiming something else is not an EPUB."""
        import zipfile

        archive = tmp_path / "wrong.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("mimetype", "application/pdf")
            z.writestr("META-INF/container.xml", "<container/>")
        assert EpubDocumentParser.score("application/zip", "wrong.zip", archive) is None

    def test_unopenable_zip_is_declined_not_raised(self, tmp_path):
        """A corrupt archive must decline quietly; Paperless reports it."""
        corrupt = tmp_path / "corrupt.zip"
        corrupt.write_bytes(b"PK\x03\x04 not really a zip")
        assert EpubDocumentParser.score("application/zip", "corrupt.zip", corrupt) is None

    def test_no_path_means_no_zip_claim(self):
        """Without a path there is nothing to inspect, so decline.

        ``path`` is optional in the protocol; guessing here would risk stealing
        a ZIP file from a parser that genuinely understands it.
        """
        assert EpubDocumentParser.score("application/zip", "x.zip") is None

    def test_empty_zip_is_declined(self, tmp_path):
        """An archive with no entries must decline, not raise."""
        import zipfile

        archive = tmp_path / "empty.zip"
        with zipfile.ZipFile(archive, "w"):
            pass
        assert EpubDocumentParser.score("application/zip", "empty.zip", archive) is None

    def test_office_documents_are_declined(self, tmp_path):
        """The gate must not steal files that merely *are* ZIP containers.

        Declaring ``application/zip`` means this parser is now offered every
        archive libmagic identifies as a ZIP: docx, odt, xlsx, jar, apk, and
        plain zip.  Each is a valid archive; none is an EPUB.  A gate that
        accepted any of them would be the same silent-failure class as the
        bug it was written to fix.
        """
        import zipfile

        for filename, entries in (
            ("report.docx", {"[Content_Types].xml", "word/document.xml"}),
            ("sheet.xlsx", {"[Content_Types].xml", "xl/workbook.xml"}),
            ("doc.odt", {"mimetype", "content.xml", "meta.xml"}),
            ("app.jar", {"META-INF/MANIFEST.MF", "com/example/Main.class"}),
        ):
            archive = tmp_path / filename
            with zipfile.ZipFile(archive, "w") as z:
                for entry in entries:
                    z.writestr(entry, "x")
            assert (
                EpubDocumentParser.score("application/zip", filename, archive) is None
            ), f"{filename} must not be claimed as an EPUB"

    def test_odt_mimetype_value_is_rejected(self, tmp_path):
        """An ODT stores ``mimetype`` too -- with a different value.

        This is the sharpest near-miss: the entry name is identical, the
        structure is similar, and only the declared value distinguishes them.
        """
        import zipfile

        archive = tmp_path / "doc.odt"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            z.writestr("META-INF/container.xml", "<container/>")
        assert EpubDocumentParser.score("application/zip", "doc.odt", archive) is None

    def test_container_xml_in_unrelated_zip_is_declined(self, tmp_path):
        """A stray ``META-INF/container.xml`` alone must not qualify a file."""
        import zipfile

        archive = tmp_path / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("META-INF/container.xml", "<container/>")
            z.writestr("readme.txt", "hello")
        assert EpubDocumentParser.score("application/zip", "bundle.zip", archive) is None

    def test_nested_container_path_is_not_a_match(self, tmp_path):
        """Only the root ``META-INF/container.xml`` is meaningful.

        A file buried in a subdirectory must not satisfy the check -- matching
        on a suffix rather than the exact path would accept archives that
        merely happen to contain a similarly named file.
        """
        import zipfile

        archive = tmp_path / "nested.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("backup/META-INF/container.xml", "<container/>")
        assert EpubDocumentParser.score("application/zip", "nested.zip", archive) is None

    def test_directory_named_container_xml_is_declined(self, tmp_path):
        """A *directory* entry must not be read as the file itself."""
        import zipfile

        archive = tmp_path / "dirlike.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml/", "")
        assert EpubDocumentParser.score("application/zip", "dirlike.zip", archive) is None

    def test_epub_mime_ignores_content_entirely(self, tmp_path):
        """A declared EPUB type is trusted without opening the file.

        Detection already said EPUB; there is nothing to second-guess, and
        doing archive I/O on the common path would be pure cost.
        """
        bogus = tmp_path / "not-really.epub"
        bogus.write_bytes(b"this is not a zip at all")
        assert EpubDocumentParser.score(EPUB_MIME, "not-really.epub", bogus) == 10

    def test_score_is_deterministic(self, tmp_path):
        """Repeated calls must agree -- no state leaking between invocations."""
        book = _write_minimal_epub(tmp_path / "book.epub")
        results = {EpubDocumentParser.score("application/zip", "book.epub", book) for _ in range(5)}
        assert results == {1}


class TestIdentityAttributes:
    """These are read by the registry *before* instantiation."""

    def test_required_attributes_present_at_class_level(self):
        for attr in ("name", "version", "author", "url"):
            value = getattr(EpubDocumentParser, attr, None)
            assert isinstance(value, str) and value, f"{attr} missing or empty"

    def test_identity_attributes_are_not_properties(self):
        """A property descriptor on the class is always truthy -- the registry
        reads these as plain attributes, so they must not be properties."""
        assert not isinstance(EpubDocumentParser.name, property)
        assert not isinstance(EpubDocumentParser.url, property)

    def test_declares_itself_local(self):
        """No content leaves the machine, so Paperless need not gate on
        remote-processing consent."""
        assert EpubDocumentParser.uses_remote_service is False


class TestCapabilityFlags:
    """Text-first by design: no archive PDF, no forced rendition."""

    def test_cannot_produce_archive(self, parser):
        assert parser.can_produce_archive is False

    def test_does_not_require_pdf_rendition(self, parser):
        assert parser.requires_pdf_rendition is False

    def test_archive_path_is_none(self, parser):
        assert parser.get_archive_path() is None

    def test_page_count_is_none(self, parser):
        """EPUB is reflowable -- a page count would be an invention."""
        assert parser.get_page_count(Path("x.epub"), EPUB_MIME) is None

    def test_date_is_none_so_paperless_parses_it(self, parser):
        assert parser.get_date() is None

    def test_extract_metadata_returns_empty_and_never_raises(self, parser):
        assert parser.extract_metadata(Path("x.epub"), EPUB_MIME) == []


class TestParse:
    def test_parse_populates_text(self, parser, stub_epub, tmp_path):
        parser.parse(tmp_path / "book.epub", EPUB_MIME)
        assert parser.get_text() == SAMPLE_TEXT

    def test_parse_tolerates_archive_flag(self, parser, stub_epub, tmp_path):
        """The consumer passes ``produce_archive``; we accept and ignore it."""
        parser.parse(tmp_path / "book.epub", EPUB_MIME, produce_archive=False)
        assert parser.get_text() == SAMPLE_TEXT

    def test_configure_is_a_noop(self, parser):
        assert parser.configure(None) is None

    def test_empty_text_is_a_parse_error(self, parser, stub_epub, tmp_path):
        """A fixed-layout or DRM'd EPUB yields no text -- report it, don't
        silently file an empty searchable document."""
        stub_epub.text = ""
        with pytest.raises(ParseError):
            parser.parse(tmp_path / "book.epub", EPUB_MIME)

    def test_converter_failure_becomes_parse_error(self, parser, stub_epub, tmp_path):
        stub_epub.raises = ValueError("not a zip file")
        with pytest.raises(ParseError) as excinfo:
            parser.parse(tmp_path / "book.epub", EPUB_MIME)
        assert "not a zip file" in str(excinfo.value)

    def test_parse_error_message_names_the_file(self, parser, stub_epub, tmp_path):
        stub_epub.raises = ValueError("boom")
        with pytest.raises(ParseError) as excinfo:
            parser.parse(tmp_path / "specific.epub", EPUB_MIME)
        assert "specific.epub" in str(excinfo.value)


class TestThumbnail:
    """The consumer calls ``get_thumbnail`` unguarded inside its error block,
    so a raise here would abort an otherwise successful consumption."""

    def test_thumbnail_is_written_and_is_webp(self, parser, stub_epub, tmp_path):
        parser.parse(tmp_path / "book.epub", EPUB_MIME)
        thumb = parser.get_thumbnail(tmp_path / "book.epub", EPUB_MIME)
        assert thumb.exists()
        assert thumb.suffix == ".webp"

    def test_thumbnail_is_valid_webp_readable_by_pillow(self, parser, stub_epub, tmp_path):
        parser.parse(tmp_path / "book.epub", EPUB_MIME)
        thumb = parser.get_thumbnail(tmp_path / "book.epub", EPUB_MIME)

        from PIL import Image

        with Image.open(thumb) as img:
            assert img.format == "WEBP"
            assert img.size == (500, 700)

    def test_thumbnail_survives_a_converter_failure(self, parser, stub_epub, tmp_path):
        """Text already extracted: a later re-read failure must not abort."""
        parser.parse(tmp_path / "book.epub", EPUB_MIME)
        stub_epub.raises = ValueError("transient")
        thumb = parser.get_thumbnail(tmp_path / "book.epub", EPUB_MIME)
        assert thumb.exists()

    def test_thumbnail_survives_when_no_text_available(self, parser, stub_epub, tmp_path):
        """Never parsed, and the converter fails -- still emit a placeholder."""
        stub_epub.raises = ValueError("unreadable")
        thumb = parser.get_thumbnail(tmp_path / "book.epub", EPUB_MIME)
        assert thumb.exists()

    def test_thumbnail_never_raises_on_empty_file(self, parser, stub_epub, tmp_path):
        stub_epub.text = ""
        thumb = parser.get_thumbnail(tmp_path / "does-not-exist.epub", EPUB_MIME)
        assert thumb.exists()


class TestLifecycle:
    def test_exit_removes_the_scratch_directory(self):
        with EpubDocumentParser() as p:
            scratch = p._tempdir
            assert scratch.exists()
        assert not scratch.exists()

    def test_exit_removes_scratch_dir_after_an_exception(self):
        scratch = None
        with pytest.raises(RuntimeError):
            with EpubDocumentParser() as p:
                scratch = p._tempdir
                raise RuntimeError("boom")
        assert scratch is not None and not scratch.exists()

    def test_instances_have_independent_scratch_dirs(self):
        with EpubDocumentParser() as a, EpubDocumentParser() as b:
            assert a._tempdir != b._tempdir

    def test_fresh_instance_reports_empty_text(self, parser):
        assert parser.get_text() == ""


class TestProtocolShape:
    """Guards against drift from the ParserProtocol surface Paperless expects."""

    def test_all_protocol_methods_present(self):
        required = (
            "parse",
            "get_text",
            "get_thumbnail",
            "get_date",
            "get_archive_path",
            "get_page_count",
            "extract_metadata",
            "configure",
            "supported_mime_types",
            "score",
            "__enter__",
            "__exit__",
        )
        for name in required:
            assert hasattr(EpubDocumentParser, name), f"missing protocol member: {name}"

    def test_no_base_class_is_required(self):
        """ParserProtocol is structural -- MRO must be plain object."""
        assert EpubDocumentParser.__mro__[1] is object
