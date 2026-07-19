class AppError(Exception):
    """Base exception for application-level errors."""


class DatabaseConnectionError(AppError):
    """Raised when the database cannot be reached."""


class MsdsNotFoundError(AppError):
    """Raised when no matching MSDS record exists for a given CAS number or chem_id."""

    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        super().__init__(f"MSDS not found for identifier: {identifier}")


class MsdsUpstreamError(AppError):
    """Raised when the KOSHA MSDS API call itself fails (auth, network, 5xx, timeout)."""

    def __init__(self, cas_no: str, reason: str) -> None:
        self.cas_no = cas_no
        self.reason = reason
        super().__init__(f"KOSHA MSDS API call failed for CAS No. {cas_no}: {reason}")


class LLMGenerationError(AppError):
    """Raised when the LLM provider fails to return a usable structured response."""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"LLM generation failed (provider={provider}): {reason}")


class CargoCategoryUnknownError(AppError):
    """Raised when a cargo has no cargo_category assigned in the Berth knowledge graph."""

    def __init__(self, chem_id: str) -> None:
        self.chem_id = chem_id
        super().__init__(f"cargo_category not assigned for chem_id: {chem_id}")
