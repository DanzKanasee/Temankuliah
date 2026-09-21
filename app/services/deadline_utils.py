"""Validation and display helpers for manually entered task deadlines."""
from datetime import datetime

from app.services.telegram_reminders import get_timezone


DEADLINE_FORMAT = "%Y-%m-%d %H:%M"


def combine_deadline_input(date_value: str, time_value: str) -> datetime:
    """Build a 24-hour local deadline from HTML date and time controls."""
    return datetime.strptime(f"{date_value} {time_value}", DEADLINE_FORMAT)


def deadline_fields(value: str) -> tuple[str, str]:
    """Provide valid date/time field values, including records made by old versions."""
    try:
        due_at = datetime.strptime(value, DEADLINE_FORMAT)
    except ValueError:
        try:
            due_at = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return "", ""
        due_at = due_at.replace(hour=23, minute=59)
    return due_at.strftime("%Y-%m-%d"), due_at.strftime("%H:%M")


def is_future_deadline(due_at: datetime, timezone_name: str) -> bool:
    return due_at > datetime.now(get_timezone(timezone_name)).replace(tzinfo=None)
