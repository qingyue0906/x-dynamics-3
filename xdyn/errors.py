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


def describe_error(exc: BaseException | None, limit: int = 600) -> str:
    """Detailed error text, including type and underlying cause.

    httpx/httpcore exceptions often have an empty str() when the socket was
    reset, so logging only the message produces blank errors.
    """
    if exc is None:
        return "unknown error"
    parts = [f"{type(exc).__name__}: {exc!r}"]
    request = None
    try:
        request = getattr(exc, "request", None)
    except Exception:
        request = None
    url = getattr(request, "url", None) if request is not None else None
    if url is not None:
        try:
            parts.append(f"{url.scheme}://{url.host}{url.path}")
        except Exception:
            pass
    cause = exc.__cause__
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        parts.append(f"<- {type(cause).__name__}: {cause!r}")
        cause = cause.__cause__
    return " | ".join(parts)[:limit]
