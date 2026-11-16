"""Validate browser attachments and prepare bounded, per-run model references."""

from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from base64 import b64decode, b64encode
from dataclasses import dataclass
from pathlib import Path

from devsquad.sandbox.pathguard import is_secret_filename

MAX_ATTACHMENTS = 3
MAX_FILE_BYTES = 256 * 1024
MAX_TEXT_BYTES = 6 * 1024
MAX_ENCODED_FILE = (MAX_FILE_BYTES + 2) // 3 * 4
MAX_UPLOAD_BODY = 1_100_000

_TEXT_TYPES = frozenset(
    ".txt .md .log .py .js .ts .jsx .tsx .vue .css .html .json .csv .yaml .yml .toml "
    ".ini .sql .sh .ps1 .java .go .rs .c .cpp .h .cs .rb .php".split()
)
_IMAGES = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}


class AttachmentError(ValueError):
    """A safe input error suitable for displaying in the browser."""


@dataclass(frozen=True)
class TaskAttachment:
    name: str
    text: str = ""
    image_base64: str = ""


def _read_binary(data: bytes, mode: str) -> bytes:
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).with_name("_attachment_reader.py")), mode],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=6,
            env={}, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        if mode == "PDF":
            raise AttachmentError(
                "PDF 无法提取文字。请检查格式、密码和页数（最多 20 页）。",
            ) from None
        raise AttachmentError("截图无法读取。请使用单帧 PNG 或 JPEG（最多 400 万像素）。") from None
    return completed.stdout


def prepare_attachment(name: str, encoded: str) -> TaskAttachment:
    if (
        name in {"", ".", ".."} or "/" in name or "\\" in name
        or any(unicodedata.category(char).startswith("C") for char in name)
    ):
        raise AttachmentError("附件文件名无效。")
    if is_secret_filename(name) or name.casefold() in {"credentials.json", "secrets.json"}:
        raise AttachmentError("密钥和环境配置文件不能作为附件。")
    suffix = Path(name).suffix.lower()
    if suffix not in _TEXT_TYPES and suffix not in _IMAGES and suffix != ".pdf":
        raise AttachmentError("请选择 UTF-8 代码或日志、PDF、PNG 或 JPEG。")
    try:
        raw = b64decode(encoded, validate=True)
    except ValueError:
        raise AttachmentError("附件编码无效。") from None
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise AttachmentError("单个附件大小必须在 1 字节至 256 KiB 之间。")
    if suffix in _IMAGES:
        jpeg = _read_binary(raw, _IMAGES[suffix])
        if not jpeg or len(jpeg) > MAX_FILE_BYTES:
            raise AttachmentError("截图处理后过大，请裁剪后再上传。")
        return TaskAttachment(name, image_base64=b64encode(jpeg).decode("ascii"))
    if suffix == ".pdf":
        if not raw.startswith(b"%PDF-"):
            raise AttachmentError("附件内容和 PDF 格式不符。")
        raw = _read_binary(raw, "PDF")
    if len(raw) > MAX_TEXT_BYTES:
        raise AttachmentError("每个文本附件或 PDF 提取出的文字最多 6 KiB，请节选关键段落。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AttachmentError("文本附件须使用 UTF-8 编码。") from None
    if not text.strip() or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise AttachmentError("附件中没有可用的文字。扫描 PDF 可以改用截图。")
    return TaskAttachment(name, text=text)
