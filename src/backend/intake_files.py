"""Turn an uploaded syllabus file into something Gemini can read.

PDFs and images go to Gemini natively as attachments. Gemini does not accept
.docx, so Word files are flattened to text here (tables included -- syllabi
keep their grading breakdown in tables). Plain-text formats are decoded.
"""

from __future__ import annotations

import io

from .gemini import Attachment

MAX_BYTES = 15 * 1024 * 1024  # inline request payloads top out around 20 MB

NATIVE = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
TEXT = {"txt", "md", "csv"}
SYLLABUS_TYPES = [*NATIVE, "docx", *sorted(TEXT)]


def _docx_to_text(data: bytes) -> str:
    from docx import Document  # noqa: PLC0415
    from docx.table import Table  # noqa: PLC0415
    from docx.text.paragraph import Paragraph  # noqa: PLC0415

    doc = Document(io.BytesIO(data))
    lines: list[str] = []
    # Walk the body in document order so a grading table stays next to the
    # heading that introduces it.
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            text = Paragraph(child, doc).text.strip()
            if text:
                lines.append(text)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, doc).rows:
                cells: list[str] = []
                for cell in row.cells:
                    t = cell.text.strip().replace("\n", " ")
                    if t and (not cells or cells[-1] != t):  # merged cells repeat
                        cells.append(t)
                if cells:
                    lines.append(" | ".join(cells))
    return "\n".join(lines)


def prepare(filename: str, data: bytes) -> tuple[str, list[Attachment]]:
    """Return ``(text, attachments)`` ready for ``ingest_syllabus``.

    Raises ``ValueError`` with a message that is safe to show the student.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not data:
        raise ValueError(f"{filename} is empty.")
    if len(data) > MAX_BYTES:
        raise ValueError(f"{filename} is over {MAX_BYTES // 1024 // 1024} MB - too big to send.")

    if ext in NATIVE:
        return "", [Attachment(filename, NATIVE[ext], data)]
    if ext == "docx":
        try:
            text = _docx_to_text(data)
        except Exception as exc:  # corrupt / not really a docx
            raise ValueError(f"{filename} could not be read as a Word file ({type(exc).__name__}).") from exc
        if not text.strip():
            raise ValueError(f"{filename} has no readable text (is it a scan? export it as PDF).")
        return text, []
    if ext in TEXT:
        return data.decode("utf-8", errors="replace"), []
    if ext == "doc":
        raise ValueError(f"{filename}: old .doc files aren't supported - save as .docx or PDF.")
    raise ValueError(f"{filename}: unsupported file type '.{ext}'.")