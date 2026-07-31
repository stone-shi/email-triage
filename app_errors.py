"""Shared exception types for the app-DB layer (users/integrations/settings/auth).

Plain exceptions rather than a framework base class, since this app has no
FastAPI-style exception-handler registry -- web_auth.py/web_api.py catch these
by type at the route boundary and translate them into the
``{"error": {"code": ..., "message": ...}}`` envelope.
"""


class AppStoreError(Exception):
    """Base class for errors raised by the *_store modules."""

    code = "error"


class NotFoundError(AppStoreError):
    code = "not_found"


class ConflictError(AppStoreError):
    code = "conflict"


class ValidationError(AppStoreError):
    code = "validation_error"


class ForbiddenError(AppStoreError):
    code = "forbidden"


class AuthRequiredError(AppStoreError):
    code = "auth_required"


class PasswordChangeRequiredError(AppStoreError):
    code = "password_change_required"
