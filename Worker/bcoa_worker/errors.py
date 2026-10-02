"""Failures a job reports with a machine-readable code."""


class JobFailure(Exception):
    def __init__(self, code: str, message: str, *, recoverable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
