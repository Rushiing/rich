"""Service-layer init.

Done here (and not in main.py) so the patch lands BEFORE any service module
imports akshare — akshare uses `requests` internally with no per-call timeout
override exposed to callers, and Railway egress to *.eastmoney.com regularly
sees individual calls that hang for 30s before failing. With 49 stocks × 3
flaky endpoints, a single bad day can stall a whole snapshot job.

We monkey-patch the global default timeout on `requests.Session.send` to
**12 seconds**: long enough that legitimate slow Chinese hosts complete,
short enough that one bad route can't park a worker for 30s. Caller-supplied
timeouts still win — anywhere we set timeout= explicitly (sina/tencent
fetchers do, with 8s) keeps that value.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_HTTP_TIMEOUT = 12  # seconds


def _install_default_http_timeout() -> None:
    try:
        import requests
        _orig_send = requests.Session.send

        def _send_with_timeout(self, request, **kwargs):
            kwargs.setdefault("timeout", DEFAULT_HTTP_TIMEOUT)
            return _orig_send(self, request, **kwargs)

        # Idempotent — don't double-wrap if reloaded.
        if not getattr(requests.Session.send, "_rich_patched", False):
            _send_with_timeout._rich_patched = True  # type: ignore[attr-defined]
            requests.Session.send = _send_with_timeout  # type: ignore[assignment]
            logger.info("services.__init__: installed default %ds HTTP timeout", DEFAULT_HTTP_TIMEOUT)
    except Exception:
        logger.exception("services.__init__: failed to install default HTTP timeout")


_install_default_http_timeout()
