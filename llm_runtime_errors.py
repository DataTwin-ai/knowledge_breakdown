"""Rate-limit check without importing the LLM SDKs (engines and the worker pool use it)."""


def is_rate_limited(exc: BaseException) -> bool:
    """True for the worker pool's RateLimited and for any provider 'too many requests' error."""
    if type(exc).__name__ == "RateLimited":
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(k in text for k in ("429", "too many requests", "rate limit", "ratelimit", "resource_exhausted",
                                   "resource exhausted", "overloaded", "529", "quota"))


FATAL = ("credit balance is too low", "plans & billing", "invalid x-api-key", "authentication_error",
         "api key not valid", "api_key_invalid", "permission_denied", "billing account", "billing is not enabled")


def is_fatal(exc: BaseException) -> bool:
    """An error no retry can fix (no credit, wrong key, billing off): every other unit would fail the same way,
    so the worker pool stops the run instead of failing unit after unit."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(k in text for k in FATAL)
