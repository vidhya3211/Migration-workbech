"""Exact evidence locators (§26). No certification without one of these."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from schemas.common import WorkbenchModel, SystemRef


class CodeLocator(WorkbenchModel):
    kind: Literal["code"] = "code"
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    symbol: str | None = None  # paragraph / procedure / Class::method

    @model_validator(mode="after")
    def _order(self) -> "CodeLocator":
        if self.line_end < self.line_start:
            raise ValueError("line_end < line_start")
        return self


class SheetLocator(WorkbenchModel):
    kind: Literal["sheet"] = "sheet"
    file: str
    sheet: str
    cell_range: str  # e.g. "B4" or "B4:D9"


class DocumentLocator(WorkbenchModel):
    kind: Literal["document"] = "document"
    file: str
    page: int | None = None
    section: str | None = None
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class SchemaLocator(WorkbenchModel):
    kind: Literal["schema"] = "schema"
    file: str
    schema_name: str | None = None
    object_name: str
    column: str | None = None
    constraint: str | None = None
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class DataLocator(WorkbenchModel):
    kind: Literal["data"] = "data"
    file: str
    row_number: int = Field(ge=1)
    record_key: str          # business key reference (pseudonymised where the key is PII)
    column: str | None = None


Locator = Annotated[
    Union[CodeLocator, SheetLocator, DocumentLocator, SchemaLocator, DataLocator], Field(discriminator="kind")
]


class Evidence(WorkbenchModel):
    """One piece of evidence behind a fact.

    ``excerpt`` is the verbatim source text at the locator (code/doc/schema/sheet) or, for customer
    data, a value reference (hash) - raw customer values are never copied into provenance.
    """

    artefact_id: str
    locator: Locator
    excerpt: str
    source_sha256: str
    system: SystemRef


def locator_label(loc: CodeLocator | SheetLocator | DocumentLocator | SchemaLocator | DataLocator) -> str:
    match loc:
        case CodeLocator():
            return f"{loc.file}:{loc.line_start}-{loc.line_end}" + (f" [{loc.symbol}]" if loc.symbol else "")
        case SheetLocator():
            return f"{loc.file}!{loc.sheet}!{loc.cell_range}"
        case DocumentLocator():
            where = f"p{loc.page} " if loc.page else ""
            return f"{loc.file} {where}§{loc.section or '-'} L{loc.line_start}-{loc.line_end}"
        case SchemaLocator():
            obj = ".".join(x for x in (loc.schema_name, loc.object_name, loc.column) if x)
            return f"{loc.file}:{loc.line_start}-{loc.line_end} {obj}" + (f" ({loc.constraint})" if loc.constraint else "")
        case DataLocator():
            return f"{loc.file} row {loc.row_number} key={loc.record_key}" + (f" col={loc.column}" if loc.column else "")
    raise TypeError(type(loc))
