import email
import imaplib
import os
import re
import json
import logging
import sqlite3
import shlex
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv
from flask import Flask, render_template, request


load_dotenv()

app = Flask(__name__)
DATABASE = os.path.join(app.instance_path, "email_cleaner.db")
logger = logging.getLogger("jev-email-cleaner")
processing_lock = threading.Lock()
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)


def get_db():
    os.makedirs(app.instance_path, exist_ok=True)
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS email_decisions (
            uid TEXT PRIMARY KEY,
            message_id TEXT,
            sender TEXT NOT NULL,
            subject TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            confidence INTEGER,
            category TEXT NOT NULL,
            user_decision TEXT,
            decided_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS email_cache (
            uid TEXT PRIMARY KEY,
            message_id TEXT,
            sender TEXT NOT NULL,
            subject TEXT NOT NULL,
            email_date TEXT NOT NULL,
            preview TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
        """
    )
    return connection


def decode_value(value, fallback="(Tanpa subjek)"):
    if not value:
        return fallback
    return str(make_header(decode_header(value)))


def email_preview(message, limit=220):
    body = ""

    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if "attachment" in (part.get("Content-Disposition") or "").lower():
            continue

        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue

        payload = part.get_payload(decode=True)
        if payload is None:
            continue

        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if content_type == "text/plain":
            body = text
            break
        if not body:
            body = re.sub(r"<[^>]+>", " ", text)

    clean_body = " ".join(body.split())
    return clean_body[:limit] + ("..." if len(clean_body) > limit else "")


def get_latest_emails(limit=None):
    address = os.getenv("GMAIL_EMAIL")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not address or not password:
        raise RuntimeError("Isi GMAIL_EMAIL dan GMAIL_APP_PASSWORD di file .env.")

    messages = []
    started_at = time.monotonic()
    logger.info("Connecting to Gmail IMAP")
    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as mailbox:
        mailbox.login(address, password)
        status, _ = mailbox.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Inbox Gmail tidak dapat dibuka.")

        status, data = mailbox.uid("search", None, "ALL")
        if status != "OK":
            raise RuntimeError("Email tidak dapat dimuat.")

        uids = data[0].split()
        if limit is not None:
            uids = uids[-limit:]
        logger.info("Found %d email(s) in Inbox", len(uids))

        uid_strings = [uid.decode() for uid in uids]
        classified_uids = set()
        cached_messages = {}
        if uid_strings:
            with get_db() as connection:
                classified_rows = connection.execute(
                    f"""
                    SELECT uid FROM email_decisions
                    WHERE uid IN ({','.join('?' for _ in uid_strings)})
                    """,
                    uid_strings,
                ).fetchall()
                cached_rows = connection.execute(
                    f"""
                    SELECT uid, message_id, sender, subject, email_date, preview
                    FROM email_cache
                    WHERE uid IN ({','.join('?' for _ in uid_strings)})
                    """,
                    uid_strings,
                ).fetchall()
            classified_uids = {row["uid"] for row in classified_rows}
            cached_messages = {
                row["uid"]: {
                    "uid": row["uid"],
                    "message_id": row["message_id"],
                    "sender": row["sender"],
                    "subject": row["subject"],
                    "date": row["email_date"],
                    "preview": row["preview"],
                }
                for row in cached_rows
                if row["uid"] not in classified_uids
            }

        messages.extend(
            cached_messages[uid.decode()]
            for uid in reversed(uids)
            if uid.decode() in cached_messages
        )
        pending_uids = [
            uid for uid in uids
            if uid.decode() not in classified_uids and uid.decode() not in cached_messages
        ]
        logger.info(
            "Fetch queue: %d Gmail, %d cached, %d already classified",
            len(pending_uids),
            len(cached_messages),
            len(classified_uids),
        )
        for index, uid in enumerate(reversed(pending_uids), 1):
            status, message_data = mailbox.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not message_data or not isinstance(message_data[0], tuple):
                logger.warning("Failed to fetch email UID %s", uid.decode())
                continue

            message = email.message_from_bytes(message_data[0][1])
            raw_date = message.get("Date")
            try:
                date = parsedate_to_datetime(raw_date).strftime("%d %b %Y, %H:%M")
            except (TypeError, ValueError, OverflowError):
                date = raw_date or "Tanggal tidak tersedia"

            parsed_message = {
                "uid": uid.decode(),
                "message_id": message.get("Message-ID", ""),
                "subject": decode_value(message.get("Subject")),
                "sender": decode_value(message.get("From"), "Pengirim tidak diketahui"),
                "date": date,
                "preview": email_preview(message) or "Tidak ada preview teks.",
            }
            messages.append(parsed_message)
            with get_db() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO email_cache (
                        uid, message_id, sender, subject, email_date, preview, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parsed_message["uid"],
                        parsed_message["message_id"],
                        parsed_message["sender"],
                        parsed_message["subject"],
                        parsed_message["date"],
                        parsed_message["preview"],
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            if index == 1 or index % 10 == 0 or index == len(pending_uids):
                logger.info(
                    "Gmail fetch progress: %d/%d (%.1f%%)",
                    index,
                    len(pending_uids),
                    index / len(pending_uids) * 100,
                )

    logger.info("Fetched %d email(s) in %.2fs", len(messages), time.monotonic() - started_at)
    return messages


def save_classifications(messages):
    with get_db() as connection:
        for message in messages:
            if "recommendation" not in message:
                continue
            connection.execute(
                """
                INSERT INTO email_decisions (
                    uid, message_id, sender, subject, recommendation, confidence, category
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET
                    message_id = excluded.message_id,
                    sender = excluded.sender,
                    subject = excluded.subject,
                    recommendation = excluded.recommendation,
                    confidence = excluded.confidence,
                    category = excluded.category
                WHERE email_decisions.user_decision IS NULL
                """,
                (
                    message["uid"],
                    message["message_id"],
                    message["sender"],
                    message["subject"],
                    message["recommendation"],
                    message["confidence"],
                    message["category"],
                ),
            )

        rows = connection.execute(
            f"SELECT uid, user_decision FROM email_decisions WHERE uid IN ({','.join('?' for _ in messages)})",
            [message["uid"] for message in messages],
        ).fetchall() if messages else []

    decisions = {row["uid"]: row["user_decision"] for row in rows}
    for message in messages:
        message["user_decision"] = decisions.get(message["uid"])


def load_existing_classifications(messages):
    if not messages:
        return []

    with get_db() as connection:
        rows = connection.execute(
            f"""
            SELECT uid, recommendation, confidence, category, user_decision
            FROM email_decisions
            WHERE uid IN ({','.join('?' for _ in messages)})
            """,
            [message["uid"] for message in messages],
        ).fetchall()

    existing = {row["uid"]: row for row in rows}
    unclassified = []
    for message in messages:
        row = existing.get(message["uid"])
        if row:
            message.update(
                recommendation=row["recommendation"],
                confidence=row["confidence"],
                category=row["category"],
                user_decision=row["user_decision"],
            )
        else:
            unclassified.append(message)
    return unclassified


def classify_in_batches(messages):
    unclassified = load_existing_classifications(messages)
    failures = []
    logger.info(
        "Classification queue: %d new, %d already known",
        len(unclassified),
        len(messages) - len(unclassified),
    )
    for index, message in enumerate(unclassified, 1):
        logger.info(
            "Processing email classification %d/%d: uid=%s",
            index,
            len(unclassified),
            message["uid"],
        )
        try:
            classify_emails([message])
            save_classifications([message])
        except RuntimeError as exc:
            failures.append(message["uid"])
            logger.error(
                "Classification skipped after retries: uid=%s error=%s",
                message["uid"],
                exc,
            )
    return failures


def classification_summary():
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT recommendation, COUNT(*) AS total
            FROM email_decisions
            GROUP BY recommendation
            """
        ).fetchall()
    counts = {"keep": 0, "review": 0, "delete": 0}
    counts.update({row["recommendation"]: row["total"] for row in rows})
    counts["classified"] = sum(counts.values())
    return counts


def trash_folder_for(mailbox):
    status, folders = mailbox.list()
    if status == "OK":
        for folder in folders or []:
            folder_name = folder.decode(errors="replace")
            if "\\Trash" in folder_name:
                return shlex.split(folder_name)[-1]
    raise RuntimeError("Folder Trash Gmail tidak ditemukan.")


def execute_recommendations(messages):
    pending = [message for message in messages if not message.get("user_decision")]
    keep_messages = [message for message in pending if message["recommendation"] == "keep"]
    delete_messages = [message for message in pending if message["recommendation"] == "delete"]
    review_messages = [message for message in pending if message["recommendation"] == "review"]
    failures = []
    logger.info(
        "Executing recommendations: keep=%d, delete=%d, review=%d",
        len(keep_messages),
        len(delete_messages),
        len(review_messages),
    )
    if review_messages:
        logger.info(
            "Review queue recorded for UID(s): %s",
            ", ".join(message["uid"] for message in review_messages),
        )

    with get_db() as connection:
        for message in keep_messages:
            connection.execute(
                "UPDATE email_decisions SET user_decision = 'keep', decided_at = ? WHERE uid = ?",
                (datetime.now(timezone.utc).isoformat(), message["uid"]),
            )
            message["user_decision"] = "keep"
            logger.info("Keep completed and recorded: uid=%s", message["uid"])

    if delete_messages:
        address = os.getenv("GMAIL_EMAIL")
        password = os.getenv("GMAIL_APP_PASSWORD")
        if not address or not password:
            raise RuntimeError("Kredensial Gmail belum lengkap.")

        with imaplib.IMAP4_SSL("imap.gmail.com", 993) as mailbox:
            mailbox.login(address, password)
            status, _ = mailbox.select("INBOX")
            if status != "OK":
                raise RuntimeError("Inbox Gmail tidak dapat dibuka untuk menjalankan rekomendasi.")
            trash_folder = trash_folder_for(mailbox)
            capabilities = {
                item.decode(errors="replace") if isinstance(item, bytes) else item
                for item in mailbox.capabilities
            }

            for message in delete_messages:
                uid = message["uid"]
                try:
                    if "MOVE" in capabilities:
                        status, _ = mailbox.uid("MOVE", uid, trash_folder)
                    else:
                        status, _ = mailbox.uid("COPY", uid, trash_folder)
                        if status == "OK":
                            status, _ = mailbox.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")
                        if status == "OK":
                            status, _ = mailbox.expunge()
                    if status != "OK":
                        raise RuntimeError("Operasi IMAP gagal.")
                except (RuntimeError, imaplib.IMAP4.error, OSError):
                    failures.append(uid)
                    logger.exception("Failed to move email UID %s to Trash", uid)
                    continue

                with get_db() as connection:
                    connection.execute(
                        "UPDATE email_decisions SET user_decision = 'delete', decided_at = ? WHERE uid = ?",
                        (datetime.now(timezone.utc).isoformat(), uid),
                    )
                message["user_decision"] = "delete"
                logger.info("Trash completed and recorded: uid=%s", uid)

    for message in messages:
        if message["recommendation"] == "delete" and message["uid"] in failures:
            message["user_decision"] = None

    summary = {
        "kept": len(keep_messages),
        "deleted": len(delete_messages) - len(failures),
        "review": sum(
            message["recommendation"] == "review" and not message.get("user_decision")
            for message in messages
        ),
        "failed": len(failures),
    }
    logger.info(
        "Actions complete: kept=%d, trashed=%d, review=%d, failed=%d",
        summary["kept"], summary["deleted"], summary["review"], summary["failed"],
    )
    return summary


def move_to_trash(uid):
    address = os.getenv("GMAIL_EMAIL")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not address or not password:
        raise RuntimeError("Kredensial Gmail belum lengkap.")

    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as mailbox:
        mailbox.login(address, password)
        status, _ = mailbox.select("INBOX")
        if status != "OK":
            raise RuntimeError("Inbox Gmail tidak dapat dibuka.")

        trash_folder = trash_folder_for(mailbox)

        capabilities = {
            item.decode(errors="replace") if isinstance(item, bytes) else item
            for item in mailbox.capabilities
        }
        if "MOVE" in capabilities:
            status, _ = mailbox.uid("MOVE", uid, trash_folder)
            if status == "OK":
                return

        status, _ = mailbox.uid("COPY", uid, trash_folder)
        if status != "OK":
            raise RuntimeError("Email gagal dipindahkan ke Trash Gmail.")
        status, _ = mailbox.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")
        if status != "OK":
            raise RuntimeError("Email tersalin ke Trash, tetapi gagal dihapus dari Inbox.")
        status, _ = mailbox.expunge()
        if status != "OK":
            raise RuntimeError("Email tersalin ke Trash, tetapi Inbox gagal diperbarui.")


def classify_emails(messages, max_attempts=3):
    api_key = os.getenv("ORVIX_API_KEY")
    if not api_key:
        raise RuntimeError("Isi ORVIX_API_KEY di file .env untuk klasifikasi email.")

    state = [
        {
            "number": index + 1,
            "sender": message["sender"],
            "subject": message["subject"],
            "preview": message["preview"],
        }
        for index, message in enumerate(messages)
    ]
    state = {
        "retention_policy": {
            "keep": [
                "invoice, receipt, payment, transfer, trade confirmation, and other financial records",
                "tax, banking, account activation, security, and identity-related messages",
                "domain or subscription expiry notices that require action",
                "certificates, course access, purchases, and durable proof of entitlement",
                "personal or work correspondence and relevant event opportunities",
            ],
            "delete": [
                "generic marketing, product promotion, discounts, and entertainment recommendations",
                "routine automated updates with no action, lasting value, or personal record",
                "stale onboarding sequences, repeated newsletters, and unwanted bulk mail",
            ],
            "rule": "When uncertain, prefer review. A notification is not disposable when it documents a transaction, grants access, concerns security, or has a deadline.",
        },
        "emails": state,
    }
    questions = {}
    for index in range(len(messages)):
        questions[f"email_{index}_disposition"] = {
            "type": "score",
            "instructions": (
                f"Nilai email nomor {index + 1}. Pilih delete hanya jika jelas tidak bernilai, "
                "seperti spam, promosi massal, atau notifikasi rutin yang usang. Pertahankan bukti "
                "transaksi, invoice, receipt, pajak, perbankan, keamanan, akses akun atau kelas, "
                "sertifikat, expiry yang perlu tindakan, korespondensi, dan peluang yang relevan. "
                "Jika ragu, pilih review. Ikuti retention_policy pada state."
            ),
            "criteria": [
                "keep: penting atau layak disimpan",
                "review: perlu diperiksa manual",
                "delete: kandidat kuat untuk dibuang",
            ],
        }
        questions[f"email_{index}_category"] = {
            "type": "choice",
            "instructions": f"Kategorikan email nomor {index + 1} berdasarkan isi yang tersedia.",
            "criteria": {
                "important": "Pribadi, pekerjaan, transaksi, keamanan, akun, atau informasi penting",
                "newsletter": "Newsletter, promosi, pemasaran, atau pembaruan produk",
                "notification": "Notifikasi rutin otomatis tanpa tindakan atau nilai arsip",
                "record": "Invoice, receipt, transaksi, pajak, sertifikat, pembelian, atau bukti permanen",
                "action_required": "Deadline, expiry, keamanan, aktivasi, atau hal yang perlu tindakan",
                "spam": "Spam, penipuan, atau pesan massal yang tidak diinginkan",
                "unclear": "Informasi tidak cukup untuk memastikan kategori",
            },
        }

    payload = json.dumps(
        {
            "model": os.getenv("ORVIX_MODEL", "orvix/jev"),
            "state": state,
            "questions": questions,
        }
    ).encode()
    base_url = os.getenv("ORVIX_BASE_URL", "https://api.orvix.id").rstrip("/")
    request_data = urllib.request.Request(
        f"{base_url}/v1/evaluate",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "jev-email-cleaner/0.1",
        },
        method="POST",
    )

    started_at = time.monotonic()
    result = None
    for attempt in range(1, max_attempts + 1):
        logger.info(
            "Sending %d email(s) to Orvix (attempt %d/%d)",
            len(messages), attempt, max_attempts,
        )
        try:
            with urllib.request.urlopen(request_data, timeout=60) as response:
                result = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            try:
                error_data = json.loads(exc.read().decode(errors="replace"))
                detail = error_data.get("error", {}).get("message") or error_data.get("message")
            except (json.JSONDecodeError, AttributeError):
                detail = None
            message = f"Orvix gagal mengevaluasi email (HTTP {exc.code})"
            if exc.code in {429, 502, 503, 504} and attempt < max_attempts:
                wait_seconds = 2 ** attempt
                logger.warning(
                    "%s; retrying in %ds (attempt %d/%d)",
                    message,
                    wait_seconds,
                    attempt,
                    max_attempts,
                )
                time.sleep(wait_seconds)
                continue
            raise RuntimeError(f"{message}: {detail}." if detail else f"{message}.") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            if attempt == max_attempts:
                raise RuntimeError(
                    f"Orvix gagal setelah {max_attempts} percobaan. Jalankan kembali untuk melanjutkan."
                ) from exc
            wait_seconds = 2 ** attempt
            logger.warning(
                "Orvix connection failed on attempt %d/%d; retrying in %ds: %s",
                attempt, max_attempts, wait_seconds, type(exc).__name__,
            )
            time.sleep(wait_seconds)

    answers = result.get("answers", {})
    usage = result.get("usage", {})
    logger.info(
        "Orvix evaluated %d email(s) in %.2fs (input=%s, output=%s tokens)",
        len(messages),
        time.monotonic() - started_at,
        usage.get("input_tokens", "unknown"),
        usage.get("output_tokens", "unknown"),
    )
    labels = ("keep", "review", "delete")
    for index, message in enumerate(messages):
        disposition = answers.get(f"email_{index}_disposition", {})
        category = answers.get(f"email_{index}_category", {})
        score = disposition.get("score")
        if not isinstance(score, (int, float)):
            message.update(recommendation="review", confidence=None, category="unclear")
            logger.info(
                "Jev result: uid=%s recommendation=review category=unclear confidence=unknown subject=%r",
                message.get("uid", "unknown"),
                message.get("subject", "")[:80],
            )
            continue

        recommendation = labels[min(2, max(0, round(score)))]
        probabilities = disposition.get("probabilities") or {}
        confidence = probabilities.get(str(labels.index(recommendation)))
        email_category = category.get("choice", "unclear")
        if email_category == "notification" and recommendation != "keep":
            recommendation = "delete"
            confidence = category.get("confidence")
        message.update(
            recommendation=recommendation,
            confidence=round(confidence * 100) if isinstance(confidence, (int, float)) else None,
            category=email_category,
        )
        logger.info(
            "Jev result: uid=%s recommendation=%s category=%s confidence=%s subject=%r",
            message.get("uid", "unknown"),
            message["recommendation"],
            message["category"],
            f"{message['confidence']}%" if message["confidence"] is not None else "unknown",
            message.get("subject", "")[:80],
        )

    return messages


@app.route('/', methods=['GET', 'POST'])
def index():
    error = None
    classification_error = None
    messages = []
    summary = classification_summary()
    loaded = request.method == 'POST'

    if loaded:
        if not processing_lock.acquire(blocking=False):
            logger.warning("Rejected duplicate bulk processing request")
            return render_template(
                'index.html',
                messages=[],
                error="Proses lain masih berjalan. Tunggu sampai selesai dan jangan refresh halaman.",
                classification_error=None,
                summary=classification_summary(),
                loaded=False,
                account=os.getenv("GMAIL_EMAIL"),
            ), 409
        started_at = time.monotonic()
        logger.info("Bulk Inbox processing started")
        try:
            messages = get_latest_emails()
            if messages:
                try:
                    failed_uids = classify_in_batches(messages)
                    summary = classification_summary()
                    messages = [
                        message for message in messages
                        if message.get("recommendation") == "review"
                    ]
                    if failed_uids:
                        classification_error = (
                            f"{len(failed_uids)} email gagal diklasifikasikan setelah retry. "
                            "Jalankan lagi untuk melanjutkan email tersebut."
                        )
                except RuntimeError as exc:
                    classification_error = str(exc)
                    logger.exception("Classification phase failed")
        except (RuntimeError, imaplib.IMAP4.error, OSError) as exc:
            error = str(exc)
            logger.exception("Gmail fetch phase failed")
        finally:
            logger.info("Bulk Inbox processing finished in %.2fs", time.monotonic() - started_at)
            processing_lock.release()

    return render_template(
        'index.html',
        messages=messages,
        error=error,
        classification_error=classification_error,
        summary=summary,
        loaded=loaded,
        account=os.getenv("GMAIL_EMAIL"),
    )


@app.post('/emails/<uid>/decision')
def decide_email(uid):
    decision = request.form.get("decision")
    logger.info("Manual decision requested: uid=%s decision=%s", uid, decision)
    if decision not in {"keep", "delete"}:
        return "Keputusan tidak valid.", 400

    with get_db() as connection:
        record = connection.execute(
            "SELECT recommendation, user_decision FROM email_decisions WHERE uid = ?",
            (uid,),
        ).fetchone()
        if not record:
            return "Email belum pernah diklasifikasikan.", 404
        if record["user_decision"] is not None:
            return "Email ini sudah diputuskan.", 409

        if decision == "delete":
            try:
                move_to_trash(uid)
            except (RuntimeError, imaplib.IMAP4.error, OSError) as exc:
                return str(exc), 502

        connection.execute(
            "UPDATE email_decisions SET user_decision = ?, decided_at = ? WHERE uid = ?",
            (decision, datetime.now(timezone.utc).isoformat(), uid),
        )

    logger.info("Manual decision completed: uid=%s decision=%s", uid, decision)
    action = "dipertahankan" if decision == "keep" else "dipindahkan ke Trash Gmail"
    return render_template(
        "decision.html",
        action=action,
        decision=decision,
    )

if __name__ == '__main__':
    app.run(debug=True)
