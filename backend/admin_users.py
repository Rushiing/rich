"""Admin CLI for user / invite management.

Usage on Railway shell (or local with DATABASE_URL set):

    # List all users + their password status
    python admin_users.py list

    # Set / reset a user's password
    python admin_users.py set-password 13800138000 'newPassw0rd'

    # Generate an invite code (optional --note + --expires-in-days)
    python admin_users.py invite create --note '老王' --expires-in-days 7

    # List invite codes (used / unused)
    python admin_users.py invite list
    python admin_users.py invite list --unused

Run inside the backend dir so `app.*` imports resolve. Railway shell:
    cd backend && python admin_users.py ...

Why a script rather than admin endpoints: keeping admin auth simple. The
script needs Railway shell access, which already gates by the env owner.
"""
from __future__ import annotations

import argparse
import secrets
import string
import sys
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import InviteCode, User
from app.services.passwords import (
    PasswordError, hash_password, validate as validate_password,
)


def _gen_code(n: int = 8) -> str:
    """Random invite code — uppercase letters + digits, ambiguous chars
    stripped (no 0/O/1/I/L) so users typing it don't fat-finger."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


# ---- user commands ----

def cmd_list_users(_args):
    with SessionLocal() as db:
        rows = db.query(User).order_by(User.id).all()
        if not rows:
            print("(no users)")
            return
        print(f"{'id':>4}  {'phone':<11}  {'pw?':<3}  created_at            last_login_at")
        for u in rows:
            pw = "yes" if u.password_hash else "—"
            ca = u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "—"
            ll = u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "—"
            print(f"{u.id:>4}  {u.phone:<11}  {pw:<3}  {ca:<19}  {ll}")


def cmd_set_password(args):
    try:
        validate_password(args.password)
    except PasswordError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    with SessionLocal() as db:
        user = db.query(User).filter(User.phone == args.phone).first()
        if user is None:
            print(f"ERROR: phone {args.phone} not found", file=sys.stderr)
            sys.exit(1)
        user.password_hash = hash_password(args.password)
        db.commit()
        print(f"OK: set password for {args.phone} (user_id={user.id})")


# ---- invite commands ----

def cmd_invite_create(args):
    code = (args.code or _gen_code()).upper()
    expires_at = None
    if args.expires_in_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=args.expires_in_days)
    # Resolve max_uses: --unlimited → NULL, --max-uses N → N, else default 1
    if args.unlimited:
        max_uses = None
    elif args.max_uses is not None:
        if args.max_uses < 1:
            print("ERROR: --max-uses must be >= 1", file=sys.stderr)
            sys.exit(2)
        max_uses = args.max_uses
    else:
        max_uses = 1
    with SessionLocal() as db:
        existing = db.query(InviteCode).filter(InviteCode.code == code).first()
        if existing:
            print(f"ERROR: code {code} already exists", file=sys.stderr)
            sys.exit(1)
        row = InviteCode(
            code=code, expires_at=expires_at, note=args.note,
            max_uses=max_uses,
        )
        db.add(row)
        db.commit()
    note_part = f"  note={args.note!r}" if args.note else ""
    exp_part = f"  expires={expires_at.strftime('%Y-%m-%d %H:%M')}" if expires_at else "  no expiry"
    uses_part = "  unlimited uses" if max_uses is None else f"  max_uses={max_uses}"
    print(f"OK: invite code {code}{uses_part}{note_part}{exp_part}")


def cmd_invite_list(args):
    with SessionLocal() as db:
        q = db.query(InviteCode)
        if args.unused:
            # "unused" now means "still has redemptions left" — for legacy
            # one-shot semantics we check current_uses < max_uses (or
            # max_uses NULL = unlimited).
            q = q.filter(
                (InviteCode.max_uses.is_(None)) |
                (InviteCode.current_uses < InviteCode.max_uses)
            )
        rows = q.order_by(InviteCode.created_at.desc()).all()
        if not rows:
            print("(no invite codes)")
            return
        print(f"{'code':<10}  {'uses':<10}  {'note':<14}  expires_at")
        for r in rows:
            cap = "∞" if r.max_uses is None else str(r.max_uses)
            uses = f"{r.current_uses or 0}/{cap}"
            note = (r.note or "")[:14]
            exp = r.expires_at.strftime("%Y-%m-%d") if r.expires_at else "—"
            print(f"{r.code:<10}  {uses:<10}  {note:<14}  {exp}")


def main():
    parser = argparse.ArgumentParser(description="rich admin CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list all users").set_defaults(func=cmd_list_users)

    sp = sub.add_parser("set-password", help="set or reset a user's password by phone")
    sp.add_argument("phone")
    sp.add_argument("password")
    sp.set_defaults(func=cmd_set_password)

    inv = sub.add_parser("invite", help="invite-code management")
    inv_sub = inv.add_subparsers(dest="invite_cmd", required=True)

    inv_create = inv_sub.add_parser("create", help="generate a new invite code")
    inv_create.add_argument("--code", help="custom code (default: random 8 chars)")
    inv_create.add_argument("--note", help="who is this for / why")
    inv_create.add_argument("--expires-in-days", type=int, help="expiry in N days (no default)")
    uses_group = inv_create.add_mutually_exclusive_group()
    uses_group.add_argument("--unlimited", action="store_true",
                            help="code can be redeemed unlimited times (通用邀请码)")
    uses_group.add_argument("--max-uses", type=int, help="cap redemptions at N (default 1, one-shot)")
    inv_create.set_defaults(func=cmd_invite_create)

    inv_list = inv_sub.add_parser("list", help="list invite codes")
    inv_list.add_argument("--unused", action="store_true", help="only show unused codes")
    inv_list.set_defaults(func=cmd_invite_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
