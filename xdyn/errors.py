class JobCancelled(Exception):
    """Raised when a running job is cancelled."""


class XApiError(RuntimeError):
    """Any non-recoverable X API error."""


class XAuthError(XApiError):
    """Cookie/session is invalid or expired."""


class XRateLimitError(XApiError):
    """X asks us to wait longer than the current job is willing to."""


class XStaleError(XApiError):
    """Query id / transaction id looks stale."""


class XTransactionError(XApiError):
    """X-Client-Transaction-Id signing material could not be loaded."""
