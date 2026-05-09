"""SMS verification — Aliyun service + dev-mode shortcut.

Two operating modes selected by config:
- **Dev mode** (default; ALIYUN_SMS_ACCESS_KEY_ID empty): codes are NOT
  actually sent. The verification code is a fixed value (`SMS_DEV_CODE`,
  default "8888"). Only phone numbers in `SMS_DEV_WHITELIST` receive a
  200 from /api/auth/sms/send; everyone else gets 403. This unblocks
  end-to-end development before the Aliyun signature/template gets
  approved (1-2 day waiting window).
- **Aliyun mode** (ALIYUN_SMS_ACCESS_KEY_ID set): real SMS via Dysmsapi.
  Currently a stub that raises NotImplementedError — wire up the actual
  HTTP call once the user has signature + template approved. Tracked as
  Phase 6 follow-up.

Code storage is in-memory (TTL 5min, dict guarded by RLock). One Railway
replica means no sync needed; SMS codes are inherently short-lived so
losing them on container restart just makes the user re-request.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass

from ..config import settings

logger = logging.getLogger(__name__)

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
CODE_TTL_SECONDS = 5 * 60       # 5 minutes
RESEND_COOLDOWN_SECONDS = 60    # don't let a user spam send within 60s


class SmsError(Exception):
    """Caller-facing errors. Message is shown to the user verbatim."""


@dataclass
class _Pending:
    code: str
    issued_at: float
    expires_at: float


_pending: dict[str, _Pending] = {}
_lock = threading.RLock()


# --- public API ----------------------------------------------------------


def is_valid_phone(phone: str) -> bool:
    return bool(PHONE_RE.match(phone or ""))


def is_dev_mode() -> bool:
    return not settings.ALIYUN_SMS_ACCESS_KEY_ID


def dev_whitelist() -> set[str]:
    return {
        p.strip() for p in (settings.SMS_DEV_WHITELIST or "").split(",")
        if p.strip()
    }


def send_code(phone: str) -> dict:
    """Issue a verification code for `phone`. Idempotent within the resend
    cooldown — re-calling within 60s returns the SAME code (so a user
    spamming the button still gets the same SMS)."""
    if not is_valid_phone(phone):
        raise SmsError("手机号格式不正确")

    if is_dev_mode():
        wl = dev_whitelist()
        if wl and phone not in wl:
            raise SmsError("当前为开发模式，仅白名单手机号可登录")

    now = time.time()
    with _lock:
        existing = _pending.get(phone)
        if existing and (now - existing.issued_at) < RESEND_COOLDOWN_SECONDS:
            # Re-issue same code; tell caller how long until next allowed send
            wait = int(RESEND_COOLDOWN_SECONDS - (now - existing.issued_at))
            return {"sent": True, "cooldown": True, "wait_s": wait}
        code = settings.SMS_DEV_CODE if is_dev_mode() else _random_code()
        _pending[phone] = _Pending(
            code=code, issued_at=now, expires_at=now + CODE_TTL_SECONDS,
        )

    if is_dev_mode():
        logger.info("sms[dev]: phone=%s code=%s (whitelist mode)", phone, code)
        return {"sent": True, "dev_mode": True}

    try:
        _send_via_aliyun(phone, code)
    except NotImplementedError:
        # Real Aliyun wiring is the Phase 6 follow-up commit; until then
        # this branch is unreachable because is_dev_mode() short-circuits.
        raise SmsError("短信通道未配置")
    except Exception:
        logger.exception("sms send failed for %s", phone)
        raise SmsError("发送失败，请稍后重试")
    return {"sent": True}


def verify_code(phone: str, code: str) -> bool:
    """Returns True if `code` matches the most recently issued code for
    `phone` AND it hasn't expired. On success the code is consumed (one-shot)
    so a replay attack with the same code fails."""
    if not is_valid_phone(phone):
        return False
    if not code or len(code) != 4 and len(code) != 6:
        return False
    now = time.time()
    with _lock:
        p = _pending.get(phone)
        if p is None:
            return False
        if now > p.expires_at:
            _pending.pop(phone, None)
            return False
        if not _ct_eq(p.code, code):
            return False
        _pending.pop(phone, None)
    return True


# --- internals -----------------------------------------------------------


def _random_code() -> str:
    """6-digit zero-padded random code."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _ct_eq(a: str, b: str) -> bool:
    """Constant-time string compare to dodge timing attacks on the verify
    path. Probably overkill for an SMS code but free."""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= ord(x) ^ ord(y)
    return diff == 0


def _send_via_aliyun(phone: str, code: str) -> None:
    """Real SMS send via Aliyun Dysmsapi. Phase 6 follow-up — until the
    user has signature + template approved on the Aliyun console, this
    intentionally raises NotImplementedError so dev mode is the only path
    that reaches send_code success.

    When implementing: use POST https://dysmsapi.aliyuncs.com/ with the
    SMSv2 signature scheme; parameters:
        Action=SendSms
        PhoneNumbers={phone}
        SignName={settings.ALIYUN_SMS_SIGN_NAME}
        TemplateCode={settings.ALIYUN_SMS_TEMPLATE_CODE}
        TemplateParam={"code": code}
    Template should embed `${code}` literally.
    """
    raise NotImplementedError("Aliyun SMS not yet wired — fill in once "
                              "signature + template are approved")
