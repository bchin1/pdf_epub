#!/usr/bin/env python3
"""
Convert large, text-based PDFs into EPUB files without loading the whole document
into memory at once.

The converter intentionally favors scalability and selectable text over exact
visual reproduction:

* Text is extracted from the PDF and written as XHTML, so it remains selectable.
* Embedded raster images are extracted once per PDF object and referenced from
  the relevant chapter pages.
* Chapters are split by a configurable page count to keep each XHTML document
  reasonably small, even for PDFs with thousands of pages.
* Temporary chapter/image files are written to disk and then packaged into a
  standards-compliant EPUB archive at the end.

This approach works well for born-digital PDFs. Scanned PDFs that contain only
page images need OCR before conversion if selectable text is required.
"""

from __future__ import annotations

import argparse
import html
import mimetypes
import re
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO

import fitz  # PyMuPDF


# EPUB readers expect XHTML/XML-safe IDs. Replacing unsupported characters keeps
# generated identifiers valid even when file names contain spaces or punctuation.
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class ChapterRecord:
    """Small metadata record retained after a chapter file is closed.

    We intentionally keep only this tiny record in memory rather than the full
    chapter content, which is important for very large source PDFs.
    """

    number: int
    title: str
    relative_path: str


@dataclass(frozen=True)
class ChapterSpec:
    """Planned chapter boundary before XHTML files are written."""

    title: str
    start_page: int
    end_page: int


@dataclass(frozen=True)
class ImageRecord:
    """Metadata for an extracted PDF image stored on disk."""

    xref: int
    relative_path: str
    media_type: str


def xml_escape(value: str) -> str:
    """Escape text for safe inclusion in XHTML/XML documents."""

    return html.escape(value, quote=True)


def normalize_paragraphs(raw_text: str) -> list[str]:
    """Convert PDF-extracted text into readable XHTML paragraphs.

    PyMuPDF returns text with line breaks that often reflect visual line wrapping
    rather than real paragraph boundaries. Joining non-empty consecutive lines
    creates more natural EPUB flow while preserving the original words as text.
    Blank lines start a new paragraph.
    """

    paragraphs: list[str] = []
    current_lines: list[str] = []

    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped:
            current_lines.append(stripped)
        elif current_lines:
            paragraphs.append(" ".join(current_lines))
            current_lines = []

    if current_lines:
        paragraphs.append(" ".join(current_lines))

    return paragraphs


def chapter_title(chapter_number: int, start_page: int, end_page: int) -> str:
    """Create a predictable chapter title from the covered page range."""

    return f"Chapter {chapter_number} (pages {start_page}-{end_page})"


def build_outline_chapter_specs(
    document: fitz.Document,
) -> list[ChapterSpec]:
    """Build chapter boundaries from top-level PDF outline entries when possible.

    PyMuPDF returns TOC entries as ``[level, title, page, ...]`` where pages are
    1-based. We intentionally use only level-1 entries as EPUB chapters:
    lower-level entries are often sections/subsections and would produce many
    tiny XHTML files if treated as full chapters.

    Invalid, duplicate, or non-increasing start pages are ignored because they
    cannot define a clean contiguous reading order. If the first usable outline
    entry begins after page 1, a "Front Matter" chapter is added so no pages are
    lost before the first named chapter.
    """

    top_level_entries: list[tuple[str, int]] = []
    last_start_page = 0

    for entry in document.get_toc():
        if len(entry) < 3:
            continue

        level, title, page = entry[:3]
        if level != 1 or not isinstance(page, int):
            continue
        if not 1 <= page <= document.page_count:
            continue
        if page <= last_start_page:
            continue

        cleaned_title = str(title).strip() or f"Chapter {len(top_level_entries) + 1}"
        top_level_entries.append((cleaned_title, page))
        last_start_page = page

    if not top_level_entries:
        return []

    specs: list[ChapterSpec] = []

    if top_level_entries[0][1] > 1:
        specs.append(
            ChapterSpec(
                title="Front Matter",
                start_page=1,
                end_page=top_level_entries[0][1] - 1,
            )
        )

    for index, (title, start_page) in enumerate(top_level_entries):
        next_start_page = (
            top_level_entries[index + 1][1]
            if index + 1 < len(top_level_entries)
            else document.page_count + 1
        )
        specs.append(
            ChapterSpec(
                title=title,
                start_page=start_page,
                end_page=next_start_page - 1,
            )
        )

    return specs


def build_fixed_size_chapter_specs(
    total_pages: int,
    pages_per_chapter: int,
) -> list[ChapterSpec]:
    """Build fallback chapter boundaries when a PDF has no usable outline."""

    specs: list[ChapterSpec] = []
    for start_page in range(1, total_pages + 1, pages_per_chapter):
        end_page = min(start_page + pages_per_chapter - 1, total_pages)
        specs.append(
            ChapterSpec(
                title=chapter_title(len(specs) + 1, start_page, end_page),
                start_page=start_page,
                end_page=end_page,
            )
        )
    return specs


def open_chapter_file(
    chapter_path: Path,
    title: str,
) -> TextIO:
    """Open a chapter XHTML file and write its reusable document header."""

    handle = chapter_path.open("w", encoding="utf-8")
    handle.write("""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head>
  <title>{title}</title>
  <link rel="stylesheet" type="text/css" href="../styles/style.css"/>
</head>
<body>
  <h1>{title}</h1>
""".format(title=xml_escape(title)))
    return handle


def close_chapter_file(handle: TextIO) -> None:
    """Finish a chapter XHTML document and close its file handle."""

    handle.write("</body>\n</html>\n")
    handle.close()


def safe_book_id(source_pdf: Path) -> str:
    """Create a stable, XML-friendly identifier stem from the PDF file name."""

    cleaned = _SAFE_ID_RE.sub("-", source_pdf.stem).strip("-")
    return cleaned or "pdf-book"


def media_type_for_extension(extension: str) -> str:
    """Return a sensible EPUB media type for an extracted image extension."""

    media_type, _ = mimetypes.guess_type(f"image.{extension}")
    return media_type or "application/octet-stream"


def write_page_xhtml(
    chapter_handle: TextIO,
    page: fitz.Page,
    page_number: int,
    image_records: dict[int, ImageRecord],
    images_dir: Path,
) -> int:
    """Write one PDF page into the current XHTML chapter.

    Returns the number of extracted text characters so the caller can warn when
    a PDF appears to be image-only. The function handles one page at a time and
    discards PyMuPDF objects immediately after use, keeping peak memory low.
    """

    raw_text = page.get_text("text", sort=True)
    paragraphs = normalize_paragraphs(raw_text)

    chapter_handle.write(f'  <section class="page" id="page-{page_number}">\n')
    chapter_handle.write(f"    <h2>Page {page_number}</h2>\n")

    if paragraphs:
        for paragraph in paragraphs:
            chapter_handle.write(f"    <p>{xml_escape(paragraph)}</p>\n")
    else:
        # Keeping a visible placeholder is more honest than silently omitting a
        # page when the PDF has no extractable text layer.
        chapter_handle.write('    <p class="no-text">[No extractable text on this page]</p>\n')

    # get_images() lists embedded raster images referenced by this page. The xref
    # is a document-wide object number, so repeated logos/backgrounds can be
    # deduplicated instead of extracted thousands of times.
    for image_tuple in page.get_images(full=True):
        xref = image_tuple[0]
        image = image_records.get(xref)

        if image is None:
            extracted = page.parent.extract_image(xref)
            extension = extracted.get("ext", "bin")
            file_name = f"image-{xref}.{extension}"
            relative_path = f"images/{file_name}"
            image_path = images_dir / file_name

            with image_path.open("wb") as image_file:
                image_file.write(extracted["image"])

            image = ImageRecord(
                xref=xref,
                relative_path=relative_path,
                media_type=media_type_for_extension(extension),
            )
            image_records[xref] = image

        chapter_handle.write(
            '    <figure><img src="../{src}" alt="Embedded image from page {page}"/></figure>\n'.format(
                src=xml_escape(image.relative_path),
                page=page_number,
            )
        )

    chapter_handle.write("  </section>\n")
    return len(raw_text.strip())


def build_nav_document(title: str, chapters: Iterable[ChapterRecord]) -> str:
    """Create EPUB 3 navigation XHTML from chapter metadata."""

    items = "\n".join(
        f'        <li><a href="{xml_escape(ch.relative_path)}">{xml_escape(ch.title)}</a></li>'
        for ch in chapters
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><title>{xml_escape(title)} - Table of Contents</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>Table of Contents</h1>
    <ol>
{items}
    </ol>
  </nav>
</body>
</html>
"""


def build_content_opf(
    title: str,
    book_uuid: str,
    chapters: list[ChapterRecord],
    images: Iterable[ImageRecord],
) -> str:
    """Create the EPUB package document (manifest + reading order)."""

    chapter_manifest = "\n".join(
        f'    <item id="chapter-{ch.number}" href="{xml_escape(ch.relative_path)}" media-type="application/xhtml+xml"/>'
        for ch in chapters
    )
    image_manifest = "\n".join(
        f'    <item id="image-{img.xref}" href="{xml_escape(img.relative_path)}" media-type="{xml_escape(img.media_type)}"/>'
        for img in images
    )
    spine = "\n".join(
        f'    <itemref idref="chapter-{ch.number}"/>' for ch in chapters
    )

    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">urn:uuid:{book_uuid}</dc:identifier>
    <dc:title>{xml_escape(title)}</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="style" href="styles/style.css" media-type="text/css"/>
{chapter_manifest}
{image_manifest}
  </manifest>
  <spine>
{spine}
  </spine>
</package>
"""


def write_epub_archive(
    output_path: Path,
    staging_root: Path,
    title: str,
    chapters: list[ChapterRecord],
    image_records: dict[int, ImageRecord],
) -> None:
    """Package staged files into a valid EPUB archive.

    EPUB requires the `mimetype` entry to be first and uncompressed. Everything
    else may be compressed normally to reduce output size.
    """

    book_uuid = str(uuid.uuid4())
    meta_inf = staging_root / "META-INF"
    oebps = staging_root / "OEBPS"
    styles_dir = oebps / "styles"

    meta_inf.mkdir(parents=True, exist_ok=True)
    styles_dir.mkdir(parents=True, exist_ok=True)

    (staging_root / "mimetype").write_text("application/epub+zip", encoding="utf-8")
    (meta_inf / "container.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        encoding="utf-8",
    )
    (oebps / "nav.xhtml").write_text(build_nav_document(title, chapters), encoding="utf-8")
    (oebps / "content.opf").write_text(
        build_content_opf(title, book_uuid, chapters, image_records.values()),
        encoding="utf-8",
    )
    (styles_dir / "style.css").write_text(
        """body { font-family: serif; line-height: 1.45; }
.page { margin-bottom: 2rem; }
h1, h2 { page-break-after: avoid; }
figure { margin: 1rem 0; text-align: center; }
img { max-width: 100%; height: auto; }
.no-text { color: #666; font-style: italic; }
""",
        encoding="utf-8",
    )

    with zipfile.ZipFile(output_path, "w") as archive:
        archive.write(staging_root / "mimetype", "mimetype", compress_type=zipfile.ZIP_STORED)

        for path in sorted(staging_root.rglob("*")):
            if path.is_dir() or path.name == "mimetype":
                continue
            archive_name = path.relative_to(staging_root).as_posix()
            archive.write(path, archive_name, compress_type=zipfile.ZIP_DEFLATED)


def convert_pdf_to_epub(
    input_pdf: Path,
    output_epub: Path,
    title: str | None = None,
    pages_per_chapter: int = 50,
) -> tuple[int, int, int]:
    """Convert a PDF to EPUB and return (pages, chapters, extracted_images)."""

    if pages_per_chapter < 1:
        raise ValueError("pages_per_chapter must be at least 1")

    resolved_title = title or input_pdf.stem
    output_epub.parent.mkdir(parents=True, exist_ok=True)

    # A TemporaryDirectory lets us stream intermediate files to disk and ensures
    # they disappear even if conversion fails halfway through.
    with tempfile.TemporaryDirectory(prefix=f"{safe_book_id(input_pdf)}-") as temp_dir:
        staging_root = Path(temp_dir)
        text_dir = staging_root / "OEBPS" / "text"
        images_dir = staging_root / "OEBPS" / "images"
        text_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)

        chapters: list[ChapterRecord] = []
        image_records: dict[int, ImageRecord] = {}
        total_text_characters = 0

        # PyMuPDF lazily loads page content as pages are requested, which keeps
        # conversion practical for documents containing many thousands of pages.
        with fitz.open(input_pdf) as document:
            total_pages = document.page_count
            chapter_specs = build_outline_chapter_specs(document)
            if not chapter_specs:
                chapter_specs = build_fixed_size_chapter_specs(total_pages, pages_per_chapter)

            # Work chapter-by-chapter but still process only one PDF page at a
            # time. Completed XHTML files are closed immediately, preserving the
            # low-memory behavior while allowing real PDF chapter boundaries.
            for chapter_number, spec in enumerate(chapter_specs, start=1):
                relative_path = f"text/chapter-{chapter_number:05d}.xhtml"
                chapter_path = staging_root / "OEBPS" / relative_path
                chapters.append(
                    ChapterRecord(
                        number=chapter_number,
                        title=spec.title,
                        relative_path=relative_path,
                    )
                )

                chapter_handle = open_chapter_file(chapter_path, spec.title)
                for page_number in range(spec.start_page, spec.end_page + 1):
                    page = document.load_page(page_number - 1)
                    total_text_characters += write_page_xhtml(
                        chapter_handle,
                        page,
                        page_number,
                        image_records,
                        images_dir,
                    )
                close_chapter_file(chapter_handle)

        write_epub_archive(output_epub, staging_root, resolved_title, chapters, image_records)

    if total_text_characters == 0:
        print(
            "Warning: no selectable text was extracted. "
            "The PDF may be scanned and may need OCR before conversion."
        )

    return total_pages, len(chapters), len(image_records)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the standalone converter."""

    parser = argparse.ArgumentParser(
        description="Convert large PDFs into EPUB while preserving extractable text."
    )
    parser.add_argument("input_pdf", type=Path, help="Path to the source PDF file")
    parser.add_argument("output_epub", type=Path, help="Path for the generated EPUB file")
    parser.add_argument("--title", help="Optional EPUB title; defaults to the PDF file name")
    parser.add_argument(
        "--pages-per-chapter",
        type=int,
        default=50,
        help="Number of PDF pages per EPUB chapter (default: 50)",
    )
    return parser.parse_args()


def main() -> None:
    """CLI entry point."""

    args = parse_args()

    if not args.input_pdf.exists():
        raise SystemExit(f"Input PDF does not exist: {args.input_pdf}")
    if args.input_pdf.suffix.lower() != ".pdf":
        raise SystemExit(f"Input file does not look like a PDF: {args.input_pdf}")

    pages, chapters, images = convert_pdf_to_epub(
        input_pdf=args.input_pdf,
        output_epub=args.output_epub,
        title=args.title,
        pages_per_chapter=args.pages_per_chapter,
    )
    print(
        f"Converted {pages} pages into {chapters} chapters, "
        f"extracting {images} unique embedded images: {args.output_epub}"
    )


if __name__ == "__main__":
    main()
