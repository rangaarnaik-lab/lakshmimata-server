"""One-off validation of the pypdf text-extraction logic used by
_generic_ann_attachment_text / _extract_pdf_text in fundamentals_worker.py.
Builds a minimal valid PDF in-memory and asserts the text comes out."""
import asyncio
import io

CONTENT = (b"BT /F1 12 Tf 72 720 Td "
           b"(Received work order worth Rs 450 Cr from NHAI for road project.) Tj ET")
OBJS = [
    b"<</Type/Catalog/Pages 2 0 R>>",
    b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
    b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>",
    b"<</Length " + str(len(CONTENT)).encode() + b">>\nstream\n" + CONTENT + b"\nendstream",
    b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
]


def build_pdf() -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(OBJS, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    startxref = len(out)
    out += b"xref\n0 " + str(len(OBJS) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (b"trailer<</Size " + str(len(OBJS) + 1).encode()
            + b"/Root 1 0 R>>\n")
    out += b"startxref\n" + str(startxref).encode() + b"\n%%EOF"
    return bytes(out)


async def main():
    from pypdf import PdfReader

    def _parse() -> str:
        reader = PdfReader(io.BytesIO(build_pdf()))
        return "\n".join((p.extract_text() or "").strip() for p in reader.pages)

    text = await asyncio.to_thread(_parse)
    print("EXTRACTED:", text)
    assert "work order" in text and "450" in text, "text extraction failed"
    print("TEST_OK")

    # ── Trim logic (mirrors _trim_pdf_pages in fundamentals_worker.py) ──
    from pypdf import PdfWriter
    writer = PdfWriter()
    for p in list(PdfReader(io.BytesIO(build_pdf())).pages) * 3:  # 3-page PDF
        writer.add_page(p)
    buf = io.BytesIO(); writer.write(buf); multi = buf.getvalue()

    def _trim(pdf, max_pages):
        reader = PdfReader(io.BytesIO(pdf))
        if len(reader.pages) <= max_pages:
            return None
        w = PdfWriter()
        for p in reader.pages[:max_pages]:
            w.add_page(p)
        b = io.BytesIO(); w.write(b); return b.getvalue()

    out = await asyncio.to_thread(_trim, multi, 2)
    assert out is not None and len(out) < len(multi), "trim failed"
    assert len(PdfReader(io.BytesIO(out)).pages) == 2, "wrong page count"
    assert await asyncio.to_thread(_trim, build_pdf(), 8) is None, "should not trim short PDF"
    print("TRIM_OK")


if __name__ == "__main__":
    asyncio.run(main())
