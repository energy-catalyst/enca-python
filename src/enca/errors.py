"""Exceptions raised by the library. All derive from :class:`EncaError`."""

from __future__ import annotations


class EncaError(Exception):
    """Base class for every error this library raises."""


class AuthError(EncaError):
    """Not signed in, the sign-in expired, or the API refused the token."""


class NotFound(EncaError):
    """The site, device or reading does not exist (or is not yours)."""


class QueryError(EncaError):
    """A history/energy query was rejected locally or by the API."""


class ApiError(EncaError):
    """The API answered with an unexpected error after all retries."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
