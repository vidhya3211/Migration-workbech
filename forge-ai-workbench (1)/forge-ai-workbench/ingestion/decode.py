"""Decode stage: bytes from the landing zone -> text with a known encoding.

Mainframe extracts can arrive as EBCDIC (cp037), so that is detected before
falling back to Windows/Latin code pages.
"""
from __future__ import annotations


class BinaryContent(Exception):
    """Raised for content that is not text (NUL bytes present)."""


def _looks_ebcdic(data: bytes) -> bool:
    sample = data[:65536]
    if not sample:
        return False
    ebcdic_spaces = sample.count(0x40)
    ascii_spaces = sample.count(0x20)
    return ebcdic_spaces > len(sample) * 0.05 and ebcdic_spaces > ascii_spaces * 3


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Return (text, encoding). Newlines are normalised to ``\\n``."""
    if b"\x00" in data[:8192]:
        raise BinaryContent()
    text: str | None = None
    encoding = ""
    try:
        text, encoding = data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        if _looks_ebcdic(data):
            text, encoding = data.decode("cp037"), "cp037"
        else:
            try:
                text, encoding = data.decode("cp1252"), "cp1252"
            except UnicodeDecodeError:
                text, encoding = data.decode("latin-1"), "latin-1"
    if encoding == "cp037":
        text = text.replace("\x85", "\n")  # EBCDIC NEL
    return text.replace("\r\n", "\n").replace("\r", "\n"), encoding
