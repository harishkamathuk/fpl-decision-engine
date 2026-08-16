"""Canonical provider failure semantics."""

from __future__ import annotations

from enum import StrEnum


class ProviderErrorCode(StrEnum):
    UNAVAILABLE = "unavailable"
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    INVALID_DATA = "invalid_data"
    MAPPING = "mapping"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"


class ProviderError(RuntimeError):
    """Base exception raised by provider adapters."""

    def __init__(
        self,
        message: str,
        *,
        code: ProviderErrorCode,
        provider_id: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_id = provider_id
        self.retryable = retryable


class ProviderUnavailableError(ProviderError):
    def __init__(self, message: str, *, provider_id: str) -> None:
        super().__init__(
            message,
            code=ProviderErrorCode.UNAVAILABLE,
            provider_id=provider_id,
            retryable=True,
        )


class ProviderAuthenticationError(ProviderError):
    def __init__(self, message: str, *, provider_id: str) -> None:
        super().__init__(
            message,
            code=ProviderErrorCode.AUTHENTICATION,
            provider_id=provider_id,
        )


class ProviderRateLimitError(ProviderError):
    def __init__(self, message: str, *, provider_id: str) -> None:
        super().__init__(
            message,
            code=ProviderErrorCode.RATE_LIMITED,
            provider_id=provider_id,
            retryable=True,
        )


class ProviderDataError(ProviderError):
    def __init__(self, message: str, *, provider_id: str) -> None:
        super().__init__(
            message,
            code=ProviderErrorCode.INVALID_DATA,
            provider_id=provider_id,
        )


class ProviderMappingError(ProviderError):
    def __init__(self, message: str, *, provider_id: str) -> None:
        super().__init__(
            message,
            code=ProviderErrorCode.MAPPING,
            provider_id=provider_id,
        )


class ProviderCapabilityError(ProviderError):
    def __init__(self, message: str, *, provider_id: str) -> None:
        super().__init__(
            message,
            code=ProviderErrorCode.UNSUPPORTED_CAPABILITY,
            provider_id=provider_id,
        )
