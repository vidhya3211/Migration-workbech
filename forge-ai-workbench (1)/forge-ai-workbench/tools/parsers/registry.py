"""File-type registry: how the Cartographer recognises an artefact (extension + content sniffing).

Adding a new file type:
  1. write the parser + an extractor agent (same ``CandidateRule`` contract every extractor follows) whose
     ``IDENTITY`` declares ``extractor=True`` and the artefact types it ``handles`` - it self-registers;
  2. ``register(FileType(...))`` here (or from the plug-in package) - extension/content -> artefact type.
Routing (artefact type -> extractor) is resolved from the agent registry at run time, so no dispatch table is
edited; a project can override routing in its settings. Nothing downstream changes: the Normaliser only cares
that every parser returns the same shape.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_M = re.IGNORECASE | re.MULTILINE
Sniffer = tuple[re.Pattern[str], float, str]


@dataclass(frozen=True)
class FileType:
    artefact_type: str
    language: str | None
    extensions: tuple[str, ...]
    sniffers: tuple[Sniffer, ...] = ()
    code: bool = False                      # extension must be confirmed by content
    classification: str = "internal"
    binary_formats: tuple[str, ...] = field(default_factory=tuple)   # zip-container / magic formats


REGISTRY: dict[str, FileType] = {}


def register(ft: FileType) -> None:
    REGISTRY[ft.artefact_type] = ft


def by_extension(ext: str) -> list[FileType]:
    return [ft for ft in REGISTRY.values() if ext in ft.extensions]


register(FileType(
    "cobol_program", "cobol", (".cbl", ".cob", ".cobol", ".pco"), code=True, sniffers=(
        (re.compile(r"^.{0,7}\b(IDENTIFICATION|ID)\s+DIVISION\b", _M), 0.5, "IDENTIFICATION DIVISION"),
        (re.compile(r"\bPROCEDURE\s+DIVISION\b", _M), 0.4, "PROCEDURE DIVISION"),
        (re.compile(r"\bPROGRAM-ID\b", _M), 0.3, "PROGRAM-ID"),
    )))
register(FileType(
    "cobol_copybook", "cobol", (".cpy", ".copy"), code=True, sniffers=(
        (re.compile(r"^[\s\d]{0,20}(01|05|10|15|20)\s+[A-Z0-9-]+\s+PIC(TURE)?\s", _M), 0.5, "level-numbered PIC entries"),
        (re.compile(r"^[\s\d]{0,24}88\s+[A-Z0-9-]+\s+VALUES?\b", _M), 0.3, "88-level condition names"),
    )))
register(FileType(
    "php", "php", (".php", ".phtml", ".inc"), code=True, sniffers=(
        (re.compile(r"<\?php"), 0.8, "<?php open tag"),
        (re.compile(r"\$this->"), 0.2, "$this-> member access"),
        (re.compile(r"\bfunction\s+\w+\s*\(", _M), 0.1, "function declarations"),
    )))
register(FileType(
    "plsql", "plsql", (".pls", ".plsql", ".pkb", ".pks", ".prc", ".fnc", ".sql"), code=True, sniffers=(
        (re.compile(r"\bCREATE\s+(OR\s+REPLACE\s+)?(PACKAGE|PROCEDURE|FUNCTION)\b", _M), 0.7, "CREATE PACKAGE/PROCEDURE/FUNCTION"),
        (re.compile(r"%TYPE\b|%ROWTYPE\b|\bDBMS_\w+", _M), 0.3, "PL/SQL type anchors / DBMS_ packages"),
        (re.compile(r":=", _M), 0.1, "PL/SQL assignment"),
    )))
register(FileType(
    "oracle_ddl", "sql", (".ddl", ".sql"), code=True, sniffers=(
        (re.compile(r"\bCREATE\s+(GLOBAL\s+TEMPORARY\s+)?TABLE\b", _M), 0.7, "CREATE TABLE"),
        (re.compile(r"\bCREATE\s+(OR\s+REPLACE\s+)?(VIEW|TRIGGER)\b|\bALTER\s+TABLE\b", _M), 0.3, "VIEW/TRIGGER/ALTER TABLE"),
        (re.compile(r"\b(VARCHAR2|NUMBER\s*\(|PRIMARY\s+KEY|NOT\s+NULL)\b", _M), 0.2, "column definitions"),
    )))
register(FileType(
    "jcl", "jcl", (".jcl", ".proc"), code=True, sniffers=(
        (re.compile(r"^//\S*\s+JOB\b", _M), 0.8, "JOB card"),
        (re.compile(r"^//\S*\s+EXEC\s+(PGM|PROC)?=?", _M), 0.5, "EXEC step"),
    )))
register(FileType(
    "spreadsheet", None, (".xlsx", ".xlsm"), binary_formats=("xlsx",)))
register(FileType(
    "document", None, (".md", ".txt", ".docx", ".pdf", ".html", ".htm", ".rtf"),
    binary_formats=("docx", "pdf")))
register(FileType(
    "data_extract", None, (".csv", ".dat", ".psv", ".tsv"), classification="confidential", sniffers=(
        (re.compile(r"\A[^\n,]+(,[^\n,]+){2,}\n[^\n]*,", re.I), 0.6, "comma-delimited header + rows"),
        (re.compile(r"\A[^\n|]+(\|[^\n|]+){2,}\n[^\n]*\|", re.I), 0.6, "pipe-delimited header + rows"),
        (re.compile(r"\A[^\n\t]+(\t[^\n\t]+){2,}\n[^\n]*\t", re.I), 0.6, "tab-delimited header + rows"),
    )))
