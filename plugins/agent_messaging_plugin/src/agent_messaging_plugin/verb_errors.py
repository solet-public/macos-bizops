from __future__ import annotations


class VerbError(Exception):
    """Shared lifecycle refusal, independent of any file snapshot mechanism."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
