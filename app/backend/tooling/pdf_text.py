"""Time-bounded PDF text extraction in a disposable local process."""
from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys

MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_PAGES = 200
MAX_TEXT = 500_000


def printed_page_label(text: str) -> str | None:
    """Keep only standalone page labels, never a numbered section heading."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    for candidate in (lines[-1], lines[0]):
        if re.fullmatch(r"(?:[ivxlcdm]+|\d{1,4})", candidate, re.I):
            return candidate
    return None


def extract_pdf(payload: bytes) -> dict:
    import pypdfium2 as pdfium
    if not payload.startswith(b"%PDF-") or len(payload) > MAX_PDF_BYTES:
        raise ValueError("Invalid or oversized PDF")
    with pdfium.PdfDocument(payload) as document:
        count = len(document)
        if count > MAX_PAGES:
            raise ValueError(f"PDF has {count} pages; the reader limit is {MAX_PAGES}")
        pages, total = [], 0
        for index in range(count):
            if total >= MAX_TEXT:
                break
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_bounded().strip()[:MAX_TEXT-total]
                finally:
                    textpage.close()
                if text:
                    pages.append({"page": index + 1, "text": text,
                                  "printed_label": printed_page_label(text)})
                    total += len(text)
            finally:
                page.close()
        if total < 120:
            raise ValueError("PDF has no usable text layer; OCR is required and was not performed")
        return {"title": "", "pages": pages, "page_count": count,
                "truncated": total >= MAX_TEXT,
                "extraction": "pdf_text_layer; OCR errors may remain in scanned text"}


def read_pdf_text(payload: bytes, *, timeout: float = 20) -> dict:
    return _run_pdf_worker("pdf_text.py", payload, timeout)


def read_remote_pdf(url: str, length: int, validator: str) -> dict:
    return _run_pdf_worker("pdf_range.py",json.dumps({"url":url,"length":length,"validator":validator}).encode(),160)


def _run_pdf_worker(name: str, payload: bytes, timeout: float) -> dict:
    root = Path(__file__).resolve().parents[3]
    private = root / ".python" / "python.exe"
    executable = str(private) if private.is_file() else sys.executable
    if "python" not in Path(executable).name.casefold():
        raise RuntimeError("No Python interpreter is available for PDF extraction")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env["PYTHONPATH"] = os.pathsep.join([str(root), str(root / ".venv" / "Lib" / "site-packages")])
    result = subprocess.run([executable, "-B", str(Path(__file__).resolve().with_name(name))],
        input=payload, capture_output=True, timeout=timeout, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    if result.returncode:
        # Callback tracebacks can precede the worker's final error. Keep the
        # cause readable in bounded source metadata instead of a partial stack.
        errors = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = errors[-1] if errors else f"worker exit code {result.returncode}"
        raise ValueError("PDF extraction failed: " + detail[-400:])
    return json.loads(result.stdout)


if __name__ == "__main__":
    try:
        result = extract_pdf(sys.stdin.buffer.read(MAX_PDF_BYTES + 1))
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except Exception as error:
        sys.stderr.write(str(error))
        raise SystemExit(1)
