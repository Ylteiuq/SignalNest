"""Finite ingestion errors shared by storage, cache and business interfaces."""

import re


class IngestError(RuntimeError):
    def __init__(
        self,
        code: str,
        stage: str,
        *,
        response_id: int | None = None,
        document_id: int | None = None,
    ):
        self.code = code
        self.stage = stage
        self.response_id = response_id
        self.document_id = document_id
        super().__init__(f"{stage}: {code} (response_id={response_id}, document_id={document_id})")

    def __str__(self) -> str:
        return (
            f"{self.stage}: {self.code} "
            f"(response_id={self.response_id}, document_id={self.document_id})"
        )


def validate_time(value: int) -> None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise IngestError("invalid_processing_time", "validation")


def validate_error_code(code: str) -> None:
    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        raise IngestError("invalid_error_code", "validation")
