"""Uploaded files becoming note text.

The tests build real PDFs and real .docx files rather than mocking the
parsers, because the failures worth catching here are format failures -- an
encrypted PDF, a .doc renamed to .docx, a scan with no text layer -- and a
mock cannot produce any of them.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from studylink import documents


# --------------------------------------------------------------- fixtures


def make_pdf(pages: list[str], encrypt_with: str | None = None) -> bytes:
    """A real PDF, with a real text layer, assembled by hand.

    Hand-assembled rather than written with a PDF library because the point of
    these tests is that extraction works on a file with genuine page objects
    and content streams. A generated file with a correct cross-reference table
    is what pypdf will actually be handed in production.
    """
    # Lay out object numbers first: 1 catalog, 2 pages, 3 font, then a
    # (page, content) pair per page.
    catalog_num, pages_num, font_num = 1, 2, 3
    page_nums, content_nums = [], []
    next_num = 4
    for _ in pages:
        page_nums.append(next_num)
        content_nums.append(next_num + 1)
        next_num += 2

    kids = b" ".join(f"{n} 0 R".encode() for n in page_nums)
    objects = [
        f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode(),
        f"<< /Type /Pages /Kids [{kids.decode()}] /Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for body, page_num, content_num in zip(pages, page_nums, content_nums):
        objects.append(
            f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> "
            f"/Contents {content_num} 0 R >>".encode()
        )
        escaped = body.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1", "replace")
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_num} 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()

    data = bytes(out)
    if encrypt_with is not None:
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
        writer.encrypt(encrypt_with)
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    return data


def make_docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        added = document.add_table(rows=len(table), cols=len(table[0]))
        for row_cells, row in zip(table, added.rows):
            for value, cell in zip(row_cells, row.cells):
                cell.text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------- text


def test_plain_text_and_markdown_come_through_unchanged():
    body = "# Week 3\n\nGradient descent minimises the loss."
    assert documents.extract("notes.md", body.encode()).text == body
    assert documents.extract("notes.txt", body.encode()).text == body


def test_a_file_exported_from_an_older_tool_still_decodes():
    """cp1252 smart quotes are not a good reason to reject a set of notes."""
    body = "The professor\u2019s point about entropy".encode("cp1252")
    assert "professor" in documents.extract("lecture.txt", body).text


def test_binary_data_named_txt_is_refused_not_turned_into_mojibake():
    """latin-1 decodes every possible byte, so without an explicit binary
    check this would succeed into noise and be stored, embedded, and
    matched against as if it were notes."""
    with pytest.raises(documents.DocumentError, match="binary"):
        documents.extract("notes.txt", bytes(range(256)) * 8)


def test_text_with_unusual_bytes_but_no_nul_still_decodes():
    """The check has to be narrow enough not to reject real notes."""
    body = bytes(range(1, 256)) # every byte except NUL
    assert documents.extract("notes.txt", body).text


# -------------------------------------------------------------------- pdf


def test_pdf_text_is_extracted():
    data = make_pdf(["Bayes rule relates conditional probabilities."])
    result = documents.extract("lecture.pdf", data)
    assert "Bayes" in result.text
    assert result.kind == "PDF"


def test_every_page_is_read_not_just_the_first():
    data = make_pdf(["page one about entropy", "page two about coding"])
    text = documents.extract("lecture.pdf", data).text
    assert "entropy" in text and "coding" in text


def test_a_scanned_pdf_says_it_is_a_scan():
    """The single most likely upload failure, and the one where a generic
    message sends someone to check a file that is fine."""
    pypdf = pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)  # image-only, in effect
    buffer = io.BytesIO()
    writer.write(buffer)

    with pytest.raises(documents.DocumentError, match="OCR"):
        documents.extract("scan.pdf", buffer.getvalue())


def test_a_password_protected_pdf_is_refused_clearly():
    data = make_pdf(["secret"], encrypt_with="hunter2")
    with pytest.raises(documents.DocumentError, match="password"):
        documents.extract("locked.pdf", data)


def test_something_that_is_not_a_pdf_named_pdf():
    with pytest.raises(documents.DocumentError, match="not a PDF"):
        documents.extract("fake.pdf", b"This is just text, honestly.")


def test_a_damaged_pdf_does_not_raise_a_stack_trace():
    with pytest.raises(documents.DocumentError):
        documents.extract("broken.pdf", b"%PDF-1.4\nthen nothing useful")


def test_a_very_long_pdf_is_truncated_and_says_so(monkeypatch):
    monkeypatch.setattr(documents, "MAX_PDF_PAGES", 2)
    data = make_pdf(["alpha", "beta", "gamma"])
    result = documents.extract("textbook.pdf", data)
    assert "gamma" not in result.text
    assert result.notice and "2 of 3" in result.notice


# ------------------------------------------------------------------- docx


def test_word_paragraphs_are_extracted():
    data = make_docx(["Entropy measures uncertainty.", "Second paragraph."])
    result = documents.extract("handout.docx", data)
    assert "Entropy measures uncertainty." in result.text
    assert result.kind == "Word"


def test_word_tables_are_extracted_too():
    """Handouts put definitions and schedules in tables, and python-docx does
    not include them in `paragraphs` -- so without this they vanish silently."""
    data = make_docx(["Reference"], table=[["Term", "Meaning"], ["MLE", "maximum likelihood"]])
    text = documents.extract("handout.docx", data).text
    assert "maximum likelihood" in text


def test_an_old_doc_renamed_to_docx_says_what_to_do():
    with pytest.raises(documents.DocumentError, match="saved as .docx"):
        documents.extract("essay.docx", b"\xd0\xcf\x11\xe0old-word-format")


def test_a_damaged_docx_is_refused_cleanly():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("not-a-word-document.txt", "hello")
    with pytest.raises(documents.DocumentError):
        documents.extract("broken.docx", buffer.getvalue())


# ------------------------------------------------------------------ limits


def test_an_unsupported_type_lists_what_is_supported():
    with pytest.raises(documents.DocumentError, match=r"\.pdf"):
        documents.extract("lecture.mp4", b"\x00\x00\x00 ftypmp42")


def test_a_file_with_no_extension_is_refused():
    with pytest.raises(documents.DocumentError):
        documents.extract("README", b"text")


def test_an_empty_file_is_refused():
    with pytest.raises(documents.DocumentError, match="empty"):
        documents.extract("notes.txt", b"")


def test_an_oversized_file_is_refused_by_size_not_content():
    with pytest.raises(documents.DocumentError, match="limit"):
        documents.extract("notes.txt", b"x" * (documents.MAX_BYTES + 1))


# ------------------------------------------------------------------ titles


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("CS101 - week 3 lecture.pdf", "CS101 - week 3 lecture"),
        ("intro_to_bayes.md", "intro to bayes"),
        ("/Users/sam/Downloads/handout.docx", "handout"),
        ("notes.final.txt", "notes.final"),
        (".hidden.txt", ".hidden"),
        ("", "Untitled upload"),
    ],
)
def test_a_readable_title_is_derived_from_the_filename(filename, expected):
    assert documents.title_from_filename(filename) == expected


def test_extraction_tidies_the_ragged_whitespace_pdfs_produce():
    """PDF extractors emit ragged spacing, and it survives into chunking
    if nothing normalises it -- wasting room in every chunk it lands in."""
    data = make_docx(["Line one   ", "", "", "   Line two\t\tspaced"])
    text = documents.extract("notes.docx", data).text
    assert "\n\n\n" not in text
    assert "   " not in text


def test_markdown_whitespace_is_left_alone():
    """The opposite rule, and the reason tidying is per-format: indentation
    and blank lines are semantic in markdown. Normalising a fenced code
    block or a nested list would quietly corrupt the note."""
    body = "# Notes\n\n```python\n    indented = True\n```\n\n- a\n    - nested\n"
    assert documents.extract("notes.md", body.encode()).text == body
