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

EPUB_MIME = "application/epub+zip"
SAMPLE_TEXT = "# Chapter One\n\n" + ("Lorem ipsum dolor sit amet. " * 200)


@pytest.fixture()
def parser():
    """A configured parser with its scratch directory cleaned up after the test."""
    with EpubDocumentParser() as p:
        yield p


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

    def test_all_declared_mime_types_score(self):
        for mime in EpubDocumentParser.supported_mime_types():
            assert EpubDocumentParser.score(mime, "book.epub") == 10

    def test_score_declines_unrelated_mime_types(self):
        """Returning None is how a parser declines a file it cannot handle."""
        assert EpubDocumentParser.score("application/pdf", "doc.pdf") is None
        assert EpubDocumentParser.score("text/plain", "notes.txt") is None
        assert EpubDocumentParser.score("", "") is None

    def test_score_accepts_optional_path_argument(self):
        """Paperless always passes ``path=``; the signature must tolerate it."""
        assert EpubDocumentParser.score(EPUB_MIME, "book.epub", Path("/tmp/book.epub")) == 10


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
