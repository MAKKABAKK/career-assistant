"""Text-only PDF intake; the original bytes remain the caller's responsibility."""
from __future__ import annotations

from io import BytesIO

MAX_PDF_BYTES = 10 * 1024 * 1024


class ResumeError(ValueError):
    """An uploaded document cannot be used as a text resume."""


def extract_pdf(data: bytes) -> str:
    """Reject oversized, non-PDF, encrypted, broken and image-only documents."""
    if not isinstance(data, bytes) or not data:
        raise ResumeError("简历文件为空。")
    if len(data) > MAX_PDF_BYTES:
        raise ResumeError("简历 PDF 不能超过 10 MB。")
    if not data.startswith(b"%PDF-"):
        raise ResumeError("文件内容不是 PDF；修改扩展名不能转换文件。")
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ResumeError("缺少 pypdf；请安装 requirements.txt 中的依赖。") from None
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ResumeError("不支持加密 PDF，请导出未加密的文本型 PDF。")
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except ResumeError:
        raise
    except Exception:
        raise ResumeError("PDF 已损坏或无法解析，请重新导出文本型 PDF。") from None
    if not any(char.isalnum() for char in text):
        raise ResumeError("PDF 没有可提取的文字；不支持空白文件或扫描件，不提供 OCR。")
    return text
