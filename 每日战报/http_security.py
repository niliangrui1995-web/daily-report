from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from settings import is_insecure_tls_allowed


def get_tls_policy_for_url(url: str) -> dict[str, Any]:
    insecure_fetch = is_insecure_tls_allowed(url)
    tls_mode = "insecure_allowlisted" if insecure_fetch else "strict"
    return {
        "verify": not insecure_fetch,
        "tls_mode": tls_mode,
        "insecure_fetch": insecure_fetch,
        "host": _safe_hostname(url),
    }


def log_tls_policy(component: str, url: str, tls_policy: dict[str, Any]) -> None:
    if tls_policy.get("insecure_fetch"):
        logging.warning(
            "[%s] allowlisted insecure TLS fallback host=%s url=%s",
            component,
            tls_policy.get("host", "unknown"),
            url,
        )


def _safe_hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
