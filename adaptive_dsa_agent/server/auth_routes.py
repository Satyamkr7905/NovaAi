# Auth routes — OTP email, password signup/login, Google ID token, JWT issue.

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete
from sqlalchemy.orm import Session

from .database import get_session
from .deps import ensure_learning_row
from .email_service import SmtpNotConfiguredError, send_otp_email
from .models import OtpCode, User
from .security import (
    create_access_token,
    generate_otp_digits,
    hash_otp,
    hash_password,
    otp_hashes_equal,
    verify_password,
)
from .settings import get_api_settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# in-process brute-force / abuse shields. reset on restart — fine for this
# app. real deploy should also have a proxy-level rate limiter (nginx, CF, slowapi).
_VERIFY_ATTEMPTS: dict[str, int] = {}
_SEND_HISTORY: dict[str, deque[float]] = {}
_RATE_LOCK = threading.Lock()


def _verify_attempts_key(email: str, code_hash: str) -> str:
    # bucket attempts per OTP so a fresh /send-otp auto-clears the counter
    # (hash changes on every new code).
    return f"{email}:{code_hash}"


def _check_send_rate(email: str) -> None:
    # raise 429 if this email is asking for OTPs too fast.
    s = get_api_settings()
    now = time.time()
    with _RATE_LOCK:
        hist = _SEND_HISTORY.setdefault(email, deque())
        # drop entries older than 24h.
        cutoff = now - 86400
        while hist and hist[0] < cutoff:
            hist.popleft()
        if hist and (now - hist[-1]) < s.otp_send_cooldown_seconds:
            retry_in = int(s.otp_send_cooldown_seconds - (now - hist[-1]))
            raise HTTPException(
                status_code=429,
                detail=f"Please wait {max(1, retry_in)}s before requesting another code.",
            )
        if len(hist) >= s.otp_send_daily_max:
            raise HTTPException(
                status_code=429,
                detail="Too many sign-in codes requested for this email today. Try again tomorrow.",
            )
        hist.append(now)


class SendOtpBody(BaseModel):
    email: EmailStr


class VerifyOtpBody(BaseModel):
    email: EmailStr
    otp: str


class GoogleBody(BaseModel):
    credential: str
    # "login"  -> only log in existing users; return needsSignup=true for new emails.
    # "signup" -> create or link the account (Google-only, no password).
    # None     -> legacy behaviour: create-or-link (kept for backward compatibility).
    mode: str | None = None


class SignupBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str | None = Field(default=None, max_length=80)


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


def _user_response(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name or u.email.split("@")[0],
        "joinedAt": u.created_at.isoformat() if u.created_at else datetime.now(timezone.utc).isoformat(),
    }


def _issue(user: User) -> dict:
    token = create_access_token(sub=user.id, email=user.email)
    return {"token": token, "user": _user_response(user)}


def _as_utc(dt: datetime) -> datetime:
    # sqlite drops tzinfo on read, so treat naive times as UTC.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _issue_otp(db: Session, email: str) -> tuple[str, str, str | None]:
    # make + save a fresh OTP for `email` and try to deliver it.
    # returns (message, delivery, dev_code_or_None) for the caller to embed.
    settings_ = get_api_settings()
    code = generate_otp_digits()
    chash = hash_otp(email, code)
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings_.otp_ttl_minutes)

    db.execute(delete(OtpCode).where(OtpCode.email == email))
    db.add(OtpCode(email=email, code_hash=chash, expires_at=exp))
    db.commit()

    try:
        send_otp_email(email, code)
        return "OTP sent to your email.", "smtp", None
    except SmtpNotConfiguredError:
        log.warning("SMTP not configured; OTP for %s is %s (dev only)", email, code)
        if settings_.is_prod():
            return (
                "Email delivery is not configured. Ask an administrator to set "
                "GMAIL_USER and GMAIL_APP_PASSWORD in the server environment.",
                "dev_inline",
                None,
            )
        return (
            "SMTP is not configured (APP_ENV=dev). Use the one-time code "
            "shown in the app to sign in.",
            "dev_inline",
            code,
        )
    except Exception as exc:
        # In local dev, SMTP can fail due to wrong app password, blocked ports,
        # or temporary provider issues. Keep auth flow usable by returning OTP
        # inline. In production this is disabled by default, but can be enabled
        # temporarily with OTP_ALLOW_INLINE_FALLBACK_IN_PROD=true.
        log.exception("Failed to send OTP email")
        if settings_.is_prod() and not settings_.otp_allow_inline_fallback_in_prod:
            raise HTTPException(
                status_code=500,
                detail="Could not send the sign-in code right now. Please try again.",
            ) from exc
        if settings_.is_prod():
            return (
                "Email delivery is temporarily unavailable. Use the one-time "
                "code shown in the app and contact support.",
                "dev_inline",
                code,
            )
        return (
            "Email delivery failed in APP_ENV=dev. Use the one-time code shown "
            "in the app to continue.",
            "dev_inline",
            code,
        )


@router.post("/send-otp")
def send_otp(body: SendOtpBody, db: Session = Depends(get_session)):
    # passwordless / account-recovery magic-link. separate from /signup.
    email = body.email.strip().lower()
    _check_send_rate(email)
    msg, delivery, dev_code = _issue_otp(db, email)
    out: dict = {"ok": True, "message": msg, "email": email, "delivery": delivery}
    if dev_code is not None:
        out["devCode"] = dev_code
    return out


@router.post("/signup")
def signup(body: SignupBody, db: Session = Depends(get_session)):
    # start an email+password account. sends an OTP to prove the email is real.
    #  - unknown email -> create unverified user w/ password_hash, send OTP.
    #  - exists but unverified -> update password_hash, resend OTP.
    #  - exists and verified -> 409 (tell them to sign in or reset).
    email = body.email.strip().lower()

    # check "already signed up" BEFORE rate limiting so a returning user gets
    # a proper "please sign in" instead of "too many requests".
    user = db.query(User).filter(User.email == email).first()
    if user and getattr(user, "email_verified", False) and user.password_hash:
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists. Please sign in.",
        )

    _check_send_rate(email)
    pwd_hash = hash_password(body.password)
    name = (body.name or "").strip() or email.split("@")[0]
    if user is None:
        user = User(email=email, name=name, password_hash=pwd_hash, email_verified=False)
        db.add(user)
    else:
        user.name = user.name or name
        user.password_hash = pwd_hash
        user.email_verified = False
        db.add(user)
    db.commit()

    msg, delivery, dev_code = _issue_otp(db, email)
    out: dict = {
        "ok": True,
        "message": "Account created. " + msg,
        "email": email,
        "delivery": delivery,
    }
    if dev_code is not None:
        out["devCode"] = dev_code
    return out


@router.post("/signup/verify")
def signup_verify(body: VerifyOtpBody, db: Session = Depends(get_session)):
    # finish signup — check OTP, flip email_verified, issue JWT.
    email = body.email.strip().lower()
    code = (body.otp or "").strip()

    if not code.isdigit() or not (4 <= len(code) <= 10):
        raise HTTPException(status_code=400, detail="Invalid OTP")

    row = db.query(OtpCode).filter(OtpCode.email == email).order_by(OtpCode.id.desc()).first()
    if row is None or _as_utc(row.expires_at) < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="OTP expired or not found")

    settings_ = get_api_settings()
    attempts_key = _verify_attempts_key(email, row.code_hash)
    with _RATE_LOCK:
        attempts = _VERIFY_ATTEMPTS.get(attempts_key, 0)
    if attempts >= settings_.otp_max_attempts:
        db.delete(row)
        db.commit()
        with _RATE_LOCK:
            _VERIFY_ATTEMPTS.pop(attempts_key, None)
        raise HTTPException(
            status_code=429,
            detail="Too many incorrect attempts. Please request a new code.",
        )

    if not otp_hashes_equal(row.code_hash, hash_otp(email, code)):
        with _RATE_LOCK:
            _VERIFY_ATTEMPTS[attempts_key] = attempts + 1
        raise HTTPException(status_code=401, detail="Invalid OTP")

    db.delete(row)
    with _RATE_LOCK:
        _VERIFY_ATTEMPTS.pop(attempts_key, None)

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        # edge case: signup row gone but OTP still hanging around.
        raise HTTPException(status_code=400, detail="Please start signup again.")
    user.email_verified = True
    db.add(user)
    db.commit()
    db.refresh(user)
    ensure_learning_row(db, user)
    return _issue(user)


@router.post("/login")
def login(body: LoginBody, db: Session = Depends(get_session)):
    # password login. only works for accounts that finished /signup/verify.
    email = body.email.strip().lower()
    user = db.query(User).filter(User.email == email).first()

    # constant-ish latency: always run bcrypt verify even if user doesn't
    # exist, so an attacker can't tell "unknown email" from "wrong password"
    # by timing.
    stored = user.password_hash if user else None
    ok_password = verify_password(body.password, stored)

    if not user or not ok_password:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.password_hash:
        raise HTTPException(
            status_code=401,
            detail="This account has no password set. Use 'Email me a code' to sign in.",
        )
    if not getattr(user, "email_verified", False):
        raise HTTPException(
            status_code=403,
            detail="Please verify your email. Check your inbox for the one-time code.",
        )

    ensure_learning_row(db, user)
    return _issue(user)


@router.post("/verify-otp")
def verify_otp(body: VerifyOtpBody, db: Session = Depends(get_session)):
    email = body.email.strip().lower()
    code = (body.otp or "").strip()

    # defense in depth — numeric only, bounded length. anyone sending junk
    # bodies shouldn't even reach the hash step.
    if not code.isdigit() or not (4 <= len(code) <= 10):
        raise HTTPException(status_code=400, detail="Invalid OTP")

    row = db.query(OtpCode).filter(OtpCode.email == email).order_by(OtpCode.id.desc()).first()
    if row is None or _as_utc(row.expires_at) < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="OTP expired or not found")

    settings_ = get_api_settings()
    attempts_key = _verify_attempts_key(email, row.code_hash)

    # kill the code after N wrong tries. 6-digit OTP = 10^6 values so 5
    # tries ~ 5e-6 chance per code.
    with _RATE_LOCK:
        attempts = _VERIFY_ATTEMPTS.get(attempts_key, 0)
    if attempts >= settings_.otp_max_attempts:
        db.delete(row)
        db.commit()
        with _RATE_LOCK:
            _VERIFY_ATTEMPTS.pop(attempts_key, None)
        raise HTTPException(
            status_code=429,
            detail="Too many incorrect attempts. Please request a new code.",
        )

    expected = hash_otp(email, code)
    if not otp_hashes_equal(row.code_hash, expected):
        with _RATE_LOCK:
            _VERIFY_ATTEMPTS[attempts_key] = attempts + 1
        raise HTTPException(status_code=401, detail="Invalid OTP")

    # success — burn the OTP and reset the counter.
    db.delete(row)
    db.commit()
    with _RATE_LOCK:
        _VERIFY_ATTEMPTS.pop(attempts_key, None)

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(email=email, name=email.split("@")[0], email_verified=True)
        db.add(user)
        db.commit()
        db.refresh(user)
    elif not getattr(user, "email_verified", False):
        user.email_verified = True
        db.add(user)
        db.commit()
        db.refresh(user)
    ensure_learning_row(db, user)
    return _issue(user)


@router.post("/auth/google")
def auth_google(body: GoogleBody, db: Session = Depends(get_session)):
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    cid = get_api_settings().google_client_id
    if not cid:
        raise HTTPException(status_code=503, detail="Google Sign-In is not configured on the server")

    try:
        info = google_id_token.verify_oauth2_token(
            body.credential,
            google_requests.Request(),
            cid,
        )
    except ValueError as exc:
        # log the full reason, return a generic message so we don't leak internals.
        log.warning("Google token verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid Google credential") from exc

    # google's lib already checks signature, audience, expiry. we add:
    #  1) trusted issuer  2) email_verified — without these, someone with an
    # unverified google account could claim any email address.
    iss = info.get("iss")
    if iss not in ("https://accounts.google.com", "accounts.google.com"):
        raise HTTPException(status_code=401, detail="Invalid Google credential")
    if not info.get("email_verified", False):
        raise HTTPException(status_code=401, detail="Google email is not verified")

    email = (info.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Google token missing email")

    sub = info.get("sub")
    name = info.get("name") or email.split("@")[0]
    picture = info.get("picture")

    user = None
    if sub:
        user = db.query(User).filter(User.google_sub == sub).first()
    if user is None:
        user = db.query(User).filter(User.email == email).first()

    mode = (body.mode or "").strip().lower()

    if user is None:
        # Login-only: never auto-create. Tell the frontend to route the user
        # through the signup page, with their Google profile pre-filled.
        if mode == "login":
            return {
                "needsSignup": True,
                "email": email,
                "name": name,
                "picture": picture,
            }
        # Signup (or legacy): create the Google-linked account and log in.
        user = User(
            email=email,
            name=name,
            picture=picture,
            google_sub=sub,
            email_verified=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        ensure_learning_row(db, user)
    else:
        user.name = name or user.name
        user.picture = picture or user.picture
        if sub and not user.google_sub:
            user.google_sub = sub
        # Google already confirmed the address, so we trust it.
        if not getattr(user, "email_verified", False):
            user.email_verified = True
        db.add(user)
        db.commit()
        db.refresh(user)

    return _issue(user)
