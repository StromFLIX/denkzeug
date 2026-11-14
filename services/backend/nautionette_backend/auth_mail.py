"""Durable, encrypted transactional account mail with bounded retries.

SMTP credentials never reach the browser or agents. The local key is independent
of the database so a database-only backup does not expose pending claim links.
"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from .auth_store import email, fail, identifier, store
from .config import settings

log = logging.getLogger(__name__)


def available() -> bool:
    if not (settings.smtp_host and settings.smtp_from and settings.public_url):
        return False
    try:
        email(settings.smtp_from)
    except HTTPException:
        return False
    return True


def validate_configuration() -> None:
    supplied = any(
        (
            settings.smtp_host,
            settings.smtp_from,
            settings.smtp_username,
            settings.smtp_password,
            settings.smtp_password_file,
        )
    )
    if supplied and not available():
        raise ValueError("Account mail requires SMTP_HOST, a valid bare SMTP_FROM mailbox and PUBLIC_URL")
    if supplied:
        if settings.smtp_password and settings.smtp_password_file:
            raise ValueError("Use either SMTP_PASSWORD_FILE or SMTP_PASSWORD, not both")
        if settings.smtp_password_file:
            Path(settings.smtp_password_file).read_bytes()  # Check readability without logging its value.
        _cipher()


def ready() -> Fernet:
    # Run before account lookup, including unknown addresses. Global configuration
    # or key-storage failure must never become an account-enumeration oracle.
    if not available():
        fail(503, "mail_unavailable", "Account email is unavailable. Contact your administrator.")
    try:
        return _cipher()
    except (OSError, ValueError):
        fail(503, "mail_unavailable", "Account email is unavailable. Contact your administrator.")


def _cipher() -> Fernet:
    path = Path(settings.data_dir) / "account-mail.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as handle:
            handle.write(Fernet.generate_key())
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        return Fernet(handle.read())


def enqueue(
    conn, address: str, subject: str, body: str, ttl: int = 1800, *, cipher: Fernet | None = None
) -> None:
    if not available():
        raise RuntimeError("Transactional account mail is not configured")
    message = EmailMessage()
    message["From"] = email(settings.smtp_from)
    message["To"] = email(address)
    message["Subject"] = subject
    message.set_content(body)
    encrypted = (cipher or _cipher()).encrypt(message.as_bytes()).decode()
    now = time.time()
    conn.execute(
        "INSERT INTO auth_mail_outbox(id,recipient,encrypted_message,created_at,expires_at) "
        "VALUES(?,?,?,?,?)",
        (identifier(), address, encrypted, now, now + ttl),
    )


def notify(conn, address: str | None, subject: str, body: str) -> None:
    """Best-effort security notice; optional SMTP must not block password recovery."""
    if not address or not available():
        return
    try:
        enqueue(conn, address, subject, body)
    except (OSError, ValueError, HTTPException):
        log.warning("Optional account security notification could not be queued")


def send(encrypted: str) -> None:
    payload = _cipher().decrypt(encrypted.encode())
    context = ssl.create_default_context()
    factory = smtplib.SMTP_SSL if settings.smtp_tls_mode == "ssl" else smtplib.SMTP
    kwargs = {"context": context} if settings.smtp_tls_mode == "ssl" else {}
    with factory(settings.smtp_host, settings.smtp_port, timeout=15, **kwargs) as smtp:
        smtp.ehlo()
        if settings.smtp_tls_mode == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        password = (
            Path(settings.smtp_password_file).read_text().rstrip("\r\n")
            if settings.smtp_password_file
            else settings.smtp_password
        )
        if settings.smtp_username:
            smtp.login(settings.smtp_username, password)
        from email import message_from_bytes

        message = message_from_bytes(payload)
        smtp.sendmail(message["From"], [message["To"]], payload)


async def deliver_once() -> None:
    now = time.time()
    store().execute(
        "UPDATE auth_mail_outbox SET state='expired',encrypted_message='' "
        "WHERE expires_at<=? AND encrypted_message!=''",
        (now,),
    )
    if not available():
        return
    for row in store().query(
        "SELECT * FROM auth_mail_outbox WHERE state='queued' AND retry_at<=? AND expires_at>? LIMIT 10",
        (now, now),
    ):
        # Claim before network I/O; after a crash requeue on startup. SMTP is at-least-once,
        # and its links are independently idempotent and single-use.
        claimed = store().execute(
            "UPDATE auth_mail_outbox SET state='sending' WHERE id=? AND state='queued'", (row["id"],)
        )
        if claimed.rowcount != 1:
            continue
        try:
            await asyncio.to_thread(send, row["encrypted_message"])
        except (OSError, smtplib.SMTPException, InvalidToken, ValueError):
            attempts = row["attempts"] + 1
            # Do not log SMTP exception text: providers can echo credentials/addresses/body.
            log.warning("Account mail delivery failed; inspect administrator delivery status")
            store().execute(
                "UPDATE auth_mail_outbox SET state=?,attempts=?,retry_at=?,error='delivery_failed',"
                "encrypted_message=CASE WHEN ?>=5 THEN '' ELSE encrypted_message END WHERE id=?",
                (
                    "failed" if attempts >= 5 else "queued",
                    attempts,
                    now + min(60 * 2**attempts, 900),
                    attempts,
                    row["id"],
                ),
            )
        else:
            store().execute(
                "UPDATE auth_mail_outbox SET state='sent',encrypted_message='',error='' WHERE id=?",
                (row["id"],),
            )


async def maintenance() -> None:
    store().execute("UPDATE auth_mail_outbox SET state='queued' WHERE state='sending'")
    while True:
        await deliver_once()
        cutoff = time.time() - 90 * 86400
        store().execute("DELETE FROM auth_audit_events WHERE created_at<?", (cutoff,))
        store().execute("DELETE FROM auth_rate_limits WHERE started_at<?", (time.time() - 86400,))
        store().execute("DELETE FROM auth_consumed_refresh WHERE expires_at<?", (time.time(),))
        store().execute("DELETE FROM auth_mail_outbox WHERE expires_at<?", (time.time() - 86400,))
        await asyncio.sleep(15)
