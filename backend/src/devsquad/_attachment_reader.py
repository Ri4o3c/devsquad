"""Parse untrusted PDF or image bytes in a resource-limited child process."""

import resource
import sys
from io import BytesIO


def pdf_text(data: bytes) -> bytes:
    from pypdf import PdfReader

    pdf = PdfReader(BytesIO(data))
    if pdf.is_encrypted or len(pdf.pages) > 20:
        raise ValueError("Encrypted PDF or page limit exceeded")
    parts = []
    for page in pdf.pages:
        parts.append(page.extract_text().strip())
    return "\n".join(parts).encode()


def jpeg_image(data: bytes, image_format: str) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(BytesIO(data), formats=[image_format]) as source:
        if source.width * source.height > 4_000_000 or getattr(source, "n_frames", 1) != 1:
            raise ValueError("Image size or frame limit exceeded")
        pixels = ImageOps.exif_transpose(source).convert("RGBA")
        pixels.thumbnail((1024, 1024))
        with Image.new("RGB", pixels.size, "white") as clean:
            clean.paste(pixels, mask=pixels.getchannel("A"))
            output = BytesIO()
            clean.save(output, format="JPEG", quality=84, optimize=True)
    return output.getvalue()


if __name__ == "__main__":
    memory_limit = 192 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    raw = sys.stdin.buffer.read(256 * 1024 + 1)
    mode = sys.argv[1]
    result = pdf_text(raw) if mode == "PDF" else jpeg_image(raw, mode)
    # The parent rejects oversized results, so it needs at most one extra byte.
    limit = 6 * 1024 if mode == "PDF" else 256 * 1024
    sys.stdout.buffer.write(result[:limit + 1])
