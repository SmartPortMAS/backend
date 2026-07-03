class AppError(Exception):
    """Base exception for application-level errors."""


class DatabaseConnectionError(AppError):
    """Raised when the database cannot be reached."""


class MsdsNotFoundError(AppError):
    """Raised when a CAS number has no matching record in KOSHA MSDS."""

    def __init__(self, cas_no: str) -> None:
        self.cas_no = cas_no
        super().__init__(f"MSDS not found for CAS No. {cas_no}")


class MsdsUpstreamError(AppError):
    """Raised when the KOSHA MSDS API call itself fails (auth, network, 5xx, timeout)."""

    def __init__(self, cas_no: str, reason: str) -> None:
        self.cas_no = cas_no
        self.reason = reason
        super().__init__(f"KOSHA MSDS API call failed for CAS No. {cas_no}: {reason}")
