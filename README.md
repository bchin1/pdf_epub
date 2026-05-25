# PDF to EPUB Converter

A small Python project for converting very large, text-based PDFs into EPUB files while keeping the text selectable and extracting embedded images.

## What it is designed for

This converter is built for documents that may contain thousands of pages, including PDFs with 7,000+ pages. It intentionally favors **scalability** and **readable reflowable text** over exact page-layout reproduction.

### Features

- Preserves extractable PDF text as selectable EPUB text
- Extracts embedded raster images and includes them in the EPUB
- Uses existing top-level PDF bookmarks as EPUB chapters when available
- Falls back to configurable page-count chapter splitting when no usable outline exists
- Processes one PDF page at a time to keep memory usage low
- Deduplicates repeated embedded images by PDF object ID
- Builds the EPUB archive directly, avoiding large in-memory book objects
- Shows a terminal progress bar during conversion

## Important limitation

If a PDF is just scanned page images and has no text layer, no converter can preserve selectable text unless OCR is performed first. This tool will still extract images, but it will warn when no selectable text is found.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python pdf_to_epub.py input.pdf output.epub
```

Optional arguments:

```bash
python pdf_to_epub.py input.pdf output.epub \
  --title "My Large Book" \
  --pages-per-chapter 75
```

During conversion, the script shows page progress and an ETA:

```text
Converting pages: [############--------] 4200/7000 (60.00%) ETA 03:12
Packaging EPUB archive...
```

To disable progress output, use:

```bash
python pdf_to_epub.py input.pdf output.epub --no-progress
```

## How chapter splitting works

If the source PDF has a usable outline / bookmark tree, the converter chooses
the outline level that most likely represents real chapters. This matters for
books that use top-level entries as containers like volumes and second-level
entries as chapters. For example, a PDF outline such as:

- Volume 1 — page 1
  - Chapter 1 — page 1
  - Chapter 2 — page 15
  - Chapter 3 — page 28
- Volume 2 — page 240

becomes chapter-level EPUB entries instead of one giant chapter per volume.

If the PDF has no usable outline bookmarks, the converter falls back to fixed
page-count splitting. By default, that means one EPUB chapter for every 50 PDF
pages:

- `chapter-00001.xhtml` contains pages 1-50
- `chapter-00002.xhtml` contains pages 51-100
- and so on

You can change the fallback split size with `--pages-per-chapter`. Smaller chapters may feel more responsive in some EPUB readers; larger chapters produce fewer files.

## Memory strategy for huge PDFs

The script is structured specifically for large inputs:

1. PyMuPDF opens the PDF and pages are loaded one at a time.
2. Each page is immediately converted into XHTML and appended to the current chapter file.
3. Completed chapter files are closed before later pages are processed.
4. Images are written to temporary files as soon as they are extracted.
5. Only compact metadata about chapters and images remains in memory.
6. The final EPUB is assembled from temporary files at the end.

That means memory usage is driven mostly by the currently loaded PDF page and any single extracted image, not by the full PDF or full EPUB content.

## Output behavior

- Text is reflowable XHTML, so it remains selectable and searchable.
- Images are appended within the page section where they were discovered.
- Original PDF page boundaries are kept as hidden XHTML anchors, but visible
  `Page N` headings are not inserted into the reading flow.
- Pages with no extractable text are recorded in a `.log` file next to the EPUB
  instead of being inserted into the book as placeholder text.
- Layout will not exactly match the original PDF; PDFs are fixed-layout documents, while EPUB is typically reflowable.
- Repeated images such as logos are stored once and reused.

## Project files

- `pdf_to_epub.py` — converter implementation and CLI
- `requirements.txt` — Python dependency list
- `README.md` — setup, usage, and design notes

## Example

```bash
python pdf_to_epub.py giant-reference.pdf giant-reference.epub --pages-per-chapter 100
```

For a 7,000-page PDF with no usable outline and `--pages-per-chapter 100`, the output EPUB will contain 70 chapter XHTML files.
