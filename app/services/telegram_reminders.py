"""Polling job for Telegram deadline reminders.

The job is deliberately database-backed: a restart does not duplicate a reminder
that Telegram has already accepted.
"""
import os
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.database.db import get_db


TIMEZONE_OPTIONS = {
    "Asia/Jakarta": "WIB (UTC+7)",
    "Asia/Makassar": "WITA (UTC+8)",
    "Asia/Jayapura": "WIT (UTC+9)",
    "UTC": "UTC (UTC+0)",
    "Asia/Singapore": "Singapore (UTC+8)",
    "Asia/Tokyo": "Tokyo (UTC+9)",
}
FALLBACK_OFFSETS = {
    "Asia/Jakarta": 7, "Asia/Makassar": 8, "Asia/Jayapura": 9,
    "UTC": 0, "Asia/Singapore": 8, "Asia/Tokyo": 9,
}


def get_timezone(timezone_name: str | None = None):
    """Resolve the chosen zone, including Windows machines without tzdata."""
    timezone_name = timezone_name if timezone_name in TIMEZONE_OPTIONS else os.getenv("APP_TIMEZONE", "Asia/Jakarta")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        offset = FALLBACK_OFFSETS.get(timezone_name, 7)
        print(f"Timezone '{timezone_name}' is unavailable; using fixed UTC{offset:+d}.")
        return timezone(timedelta(hours=offset), name=TIMEZONE_OPTIONS.get(timezone_name, "WIB"))


TIMEZONE = get_timezone()
REMINDERS = (
    ("h-3", timedelta(days=3), "3 hari lagi"),
    ("h-1", timedelta(days=1), "besok"),
    ("10-menit", timedelta(minutes=10), "10 menit lagi"),
)


def send_telegram_message(chat_id: str, message: str) -> bool:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token or not chat_id:
        return False
    payload = urlencode({"chat_id": chat_id, "text": message}).encode()
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except (URLError, OSError) as exc:
        print(f"Telegram reminder failed: {exc}")
        return False


def send_due_reminders() -> None:
    """Send each relevant reminder once while its deadline has not passed."""
    now = datetime.now(TIMEZONE)
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT d.id, d.tugas_name, d.deadline_date, d.timezone, u.telegram_chat_id
            FROM deadlines d JOIN users u ON u.id = d.user_id
            WHERE d.status = 'pending' AND u.telegram_chat_id IS NOT NULL
                  AND TRIM(u.telegram_chat_id) <> ''
            """
        ).fetchall()
        for row in rows:
            try:
                deadline = datetime.strptime(row["deadline_date"], "%Y-%m-%d %H:%M").replace(
                    tzinfo=get_timezone(row["timezone"])
                )
            except ValueError:
                continue
            if deadline <= now:
                continue
            for reminder_type, offset, human_time in REMINDERS:
                reminder_at = deadline - offset
                # Reminder targets are intended to be sent exactly at H-3, H-1, and
                # 10 minutes before the deadline. We allow a small scheduling tolerance
                # so brief restarts or delayed polling do not miss a valid send window.
                tolerance = timedelta(minutes=5)
                if now < reminder_at - tolerance or now > reminder_at + tolerance:
                    continue
                already_sent = conn.execute(
                    "SELECT 1 FROM deadline_notifications WHERE deadline_id = ? AND reminder_type = ?",
                    (row["id"], reminder_type),
                ).fetchone()
                if already_sent:
                    continue
                message = (
                    f"Pengingat tugas: {row['tugas_name']}\n"
                    f"Deadline: {deadline.strftime('%d %B %Y, %H:%M')}\n"
                    f"Waktu tersisa: {human_time}."
                )
                if send_telegram_message(str(row["telegram_chat_id"]), message):
                    conn.execute(
                        "INSERT INTO deadline_notifications (deadline_id, reminder_type) VALUES (?, ?)",
                        (row["id"], reminder_type),
                    )
                    conn.commit()
    finally:
        conn.close()
