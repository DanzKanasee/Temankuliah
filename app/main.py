import hashlib
import hmac
import os
import secrets
import shutil
import re
import asyncio
import json
from contextlib import suppress
from html import escape
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.database.db import get_db, init_db
from app.services.deadline_utils import combine_deadline_input, deadline_fields, is_future_deadline
from app.services.telegram_reminders import TIMEZONE_OPTIONS, send_due_reminders
from app.utils.gemini_helper import extract_deadlines, extract_schedule_entries, extract_schedule_from_image, generate_formula_explanation, generate_speaker_notes, generate_summary, translate_to_english
from app.utils.parser import extract_text_from_file

load_dotenv()
app = FastAPI(title="AI Agent Mahasiswa Web")
COOKIE_SECRET = os.getenv("SESSION_SECRET", secrets.token_urlsafe(32)).encode()
PASSWORD_ITERATIONS = 600_000
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR.parent / "uploads"
if os.getenv("VERCEL"):
    UPLOAD_DIR = Path("/tmp/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx"}
SCHEDULE_EXTENSIONS = {".pdf", ".docx", ".png", ".jpg", ".jpeg"}
DAY_ORDER = {"SENIN": 0, "SELASA": 1, "RABU": 2, "KAMIS": 3, "JUMAT": 4, "SABTU": 5, "MINGGU": 6}
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
reminder_task: asyncio.Task | None = None


def format_ai_result(text: str) -> Markup:
    """Render a safe Markdown subset so AI output is comfortable to read."""
    lines = escape(text or "").splitlines()
    rendered: list[str] = []
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            rendered.append("</ul>")
            in_list = False

    def inline(value: str) -> str:
        value = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", value)
        value = re.sub(r"`(.+?)`", r"<code>\1</code>", value)
        return value

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            close_list()
        elif line in {"---", "***"}:
            close_list()
            rendered.append("<hr class='my-6 border-slate-100'>")
        elif match := re.match(r"^(#{1,3})\s+(.+)$", line):
            close_list()
            level = len(match.group(1)) + 2
            styles = {3: "text-xl mt-7", 4: "text-lg mt-6", 5: "text-base mt-5"}
            rendered.append(f"<h{level} class='{styles[level]} font-bold text-slate-900 leading-snug mb-2'>{inline(match.group(2))}</h{level}>")
        elif match := re.match(r"^(?:[-*]|\d+[.)])\s+(.+)$", line):
            if not in_list:
                rendered.append("<ul class='my-3 space-y-2 pl-5 list-disc marker:text-indigo-500'>")
                in_list = True
            rendered.append(f"<li class='pl-1'>{inline(match.group(1))}</li>")
        else:
            close_list()
            rendered.append(f"<p class='mb-3'>{inline(line)}</p>")
    close_list()
    return Markup("\n".join(rendered))


@app.on_event("startup")
async def startup_event():
    global reminder_task
    init_db()
    if not os.getenv("VERCEL"):
        reminder_task = asyncio.create_task(reminder_loop())


@app.on_event("shutdown")
async def shutdown_event():
    if reminder_task:
        reminder_task.cancel()
        with suppress(asyncio.CancelledError):
            await reminder_task


async def reminder_loop():
    while True:
        try:
            send_due_reminders()
        except Exception as exc:
            # Keep the scheduler alive if one polling cycle has a transient failure.
            print(f"Deadline reminder cycle failed: {exc}")
        await asyncio.sleep(30)


@app.get("/api/cron/reminders")
async def cron_reminders(request: Request):
    """Run reminders from a Vercel Cron request instead of a persistent worker."""
    cron_secret = os.getenv("CRON_SECRET")
    authorization = request.headers.get("authorization", "")
    if cron_secret and authorization != f"Bearer {cron_secret}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        send_due_reminders()
    except RuntimeError as exc:
        print(f"Cron reminder cycle failed: {exc}")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        print(f"Cron reminder cycle failed: {exc}")
        raise HTTPException(status_code=500, detail="Reminder cycle failed") from exc
    return {"ok": True}


def current_user_id(request: Request) -> int | None:
    value = request.cookies.get("user_session", "")
    try:
        user_id, signature = value.split(".", maxsplit=1)
        expected = hmac.new(COOKIE_SECRET, user_id.encode(), hashlib.sha256).hexdigest()
        return int(user_id) if hmac.compare_digest(signature, expected) else None
    except (TypeError, ValueError):
        return None


def session_value(user_id: int) -> str:
    value = str(user_id)
    signature = hmac.new(COOKIE_SECRET, value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{signature}"


def hash_password(password: str) -> str:
    """Create a salted PBKDF2 hash; passwords are never stored in plain text."""
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${derived.hex()}"


def password_matches(password: str, stored: str | None) -> bool:
    try:
        algorithm, iterations, salt_hex, expected_hex = (stored or "").split("$", maxsplit=3)
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(derived.hex(), expected_hex)
    except (TypeError, ValueError):
        return False


def auth_form_error(template_name: str, request: Request, error: str, form_data: dict | None = None):
    return templates.TemplateResponse(
        request=request, name=template_name,
        context={"error": error, "form_data": form_data or {}}, status_code=422,
    )


def make_dashboard_context(request: Request, user_id: int, last_result: str | None = None, error: str | None = None,
                           active_result_id: int | None = None, result_title: str | None = None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        return None
    cursor.execute("SELECT COUNT(*) FROM summaries WHERE user_id = ?", (user_id,))
    total_rangkuman = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM deadlines WHERE user_id = ? AND status = 'pending'", (user_id,))
    deadline_pending = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM deadlines WHERE user_id = ? AND status = 'completed'", (user_id,))
    tugas_selesai = cursor.fetchone()[0]
    cursor.execute("SELECT * FROM summaries WHERE user_id = ? ORDER BY id DESC", (user_id,))
    all_results = [dict(row) for row in cursor.fetchall()]
    riwayat_rangkuman = [item for item in all_results if item["action_type"] == "Rangkuman"]
    riwayat_naskah = [item for item in all_results if item["action_type"] == "Naskah Presentasi"]
    riwayat_rumus = [item for item in all_results if item["action_type"] == "Bedah Rumus"]
    cursor.execute("SELECT * FROM deadlines WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,))
    daftar_deadlines = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"request": request, "user_name": user["name"], "total_rangkuman": total_rangkuman,
            "deadline_pending": deadline_pending, "tugas_selesai": tugas_selesai,
            "riwayat_rangkuman": riwayat_rangkuman, "riwayat_naskah": riwayat_naskah,
            "riwayat_rumus": riwayat_rumus, "daftar_deadlines": daftar_deadlines,
            "timezone_options": TIMEZONE_OPTIONS,
            "last_result": format_ai_result(last_result) if last_result else None, "error": error,
            "active_result_id": active_result_id, "result_title": result_title}


TRUSTED_SCHOLARSHIPS = (
    {"name": "KIP Kuliah", "audience": "Mahasiswa D4/S1; prioritas bagi yang membutuhkan dukungan ekonomi.",
     "focus": "semua jurusan", "url": "https://kip-kuliah.kemdiktisaintek.go.id/", "source": "Portal resmi Kemdiktisaintek"},
    {"name": "Beasiswa LPDP", "audience": "Lulusan S1/S2 untuk studi lanjut S2/S3.",
     "focus": "STEM, sosial-humaniora, seni, pendidikan, kesehatan", "url": "https://lpdp.kemenkeu.go.id/beasiswa/pendaftaran-beasiswa/", "source": "Portal resmi LPDP, Kementerian Keuangan"},
    {"name": "Beasiswa Bank Indonesia", "audience": "Mahasiswa aktif pada perguruan tinggi mitra Bank Indonesia.",
     "focus": "semua jurusan; cek kuota dan mitra kampus", "url": "https://www.bi.go.id/id/fungsi-utama/sdm/beasiswa/Default.aspx", "source": "Situs resmi Bank Indonesia"},
    {"name": "Beasiswa Unggulan", "audience": "Mahasiswa berprestasi yang memenuhi ketentuan program saat pendaftaran dibuka.",
     "focus": "semua jurusan", "url": "https://beasiswaunggulan.kemdikbud.go.id/", "source": "Portal resmi kementerian"},
)


def trusted_scholarships_for(major: str) -> list[dict]:
    """Return only a reviewed catalogue; do not surface unverified web listings."""
    major = major.lower()
    priority_terms = {"teknik", "informatika", "komputer", "data", "sains", "matematika", "kesehatan", "farmasi"}
    is_stem = any(term in major for term in priority_terms)
    items = [dict(item) for item in TRUSTED_SCHOLARSHIPS]
    if is_stem:
        items[1]["recommendation"] = "Relevan untuk bidang STEM; cek skema dan bidang prioritas yang sedang dibuka."
    else:
        items[1]["recommendation"] = "Untuk studi lanjut; cek kategori SHARE atau skema bidang yang tersedia."
    for item in items:
        item.setdefault("recommendation", f"Pertimbangkan jika sesuai syarat program dan jurusan {major.title()}.")
    return items


def _coerce_schedule_day(value: str) -> str:
    day = str(value or "").upper().strip()
    day = re.sub(r"[^A-Z]", "", day)
    day_aliases = {
        "MONDAY": "SENIN", "TUESDAY": "SELASA", "WEDNESDAY": "RABU", "THURSDAY": "KAMIS",
        "FRIDAY": "JUMAT", "FRI": "JUMAT", "JUM": "JUMAT", "JUMAT": "JUMAT", "JUMATNYA": "JUMAT",
        "SATURDAY": "SABTU", "SUNDAY": "MINGGU",
    }
    return day_aliases.get(day, day)


def fallback_parse_schedule_entries(raw_text: str) -> list[dict]:
    """Parse raw schedule text extracted from PDF/Word when Gemini returns invalid JSON."""
    if not raw_text:
        return []

    entries: list[dict] = []
    current_day: str | None = None
    raw_lines = [line.strip() for line in raw_text.replace("\r", "\n").split("\n")]

    pending_course = ""
    pending_slot = None

    for line in raw_lines:
        if not line:
            continue

        normalized = re.sub(r"\s+", " ", line).strip()
        if not normalized:
            continue

        day_match = re.search(r"\b(SENIN|SELASA|RABU|KAMIS|JUMAT|SABTU|MINGGU)\b", normalized, flags=re.IGNORECASE)
        if day_match:
            current_day = _coerce_schedule_day(day_match.group(1))
            if current_day not in DAY_ORDER:
                current_day = None
            pending_course = ""
            pending_slot = None
            continue

        time_match = re.search(r"(\d{1,2})\s*[:.]\s*(\d{2})\s*(?:-|–|—|s/d|sampai)\s*(\d{1,2})\s*[:.]\s*(\d{2})", normalized, flags=re.IGNORECASE)
        if time_match and current_day:
            start = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
            end = f"{int(time_match.group(3)):02d}:{time_match.group(4)}"
            pending_slot = (start, end)
            remainder = normalized[time_match.end():].strip()
            if remainder:
                pending_course = remainder.lstrip("-:|").strip()
                course = pending_course.strip()
                room = ""
                lecturer = ""
                class_name = ""
                parts = [part.strip() for part in re.split(r"\s*\|\s*|\s*[-–—]\s*", course) if part.strip()]
                if parts:
                    course = parts[0]
                if len(parts) > 1:
                    candidate = parts[1]
                    if re.search(r"(?:[A-Z]|\d{2,})", candidate):
                        room = candidate
                    else:
                        class_name = candidate
                if len(parts) > 2:
                    lecturer = parts[2]
                if len(parts) > 3:
                    class_name = parts[3]
                entries.append({
                    "day": current_day,
                    "start": pending_slot[0],
                    "end": pending_slot[1],
                    "course": course,
                    "room": room,
                    "lecturer": lecturer,
                    "class": class_name,
                })
                pending_course = ""
                pending_slot = None
            continue

        if current_day and pending_slot:
            if not pending_course:
                pending_course = normalized.lstrip("-:|").strip()
            else:
                pending_course = f"{pending_course} {normalized}".strip()

        if not current_day or not pending_slot:
            continue

        course = pending_course.strip()
        if not course:
            continue

        room = ""
        lecturer = ""
        class_name = ""
        parts = [part.strip() for part in re.split(r"\s*\|\s*|\s*[-–—]\s*", course) if part.strip()]
        if parts:
            course = parts[0]
        if len(parts) > 1:
            candidate = parts[1]
            if re.search(r"(?:[A-Z]|\d{2,})", candidate):
                room = candidate
            else:
                class_name = candidate
        if len(parts) > 2:
            lecturer = parts[2]
        if len(parts) > 3:
            class_name = parts[3]

        entries.append({
            "day": current_day,
            "start": pending_slot[0],
            "end": pending_slot[1],
            "course": course,
            "room": room,
            "lecturer": lecturer,
            "class": class_name,
        })
        pending_course = ""
        pending_slot = None

    return entries


def parse_schedule_entries(response_text: str) -> list[dict]:
    """Validate the AI response before it is allowed into the user's calendar."""
    if not response_text:
        return []
    try:
        cleaned = response_text.replace("```json", "").replace("```", "").strip()
        # Models sometimes add one sentence before/after an otherwise valid array.
        match = re.search(r"\[\s*\{.*\}\s*\]", cleaned, flags=re.DOTALL)
        values = json.loads(match.group(0) if match else cleaned)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback_parse_schedule_entries(response_text)
    if isinstance(values, dict):
        wrapped_values = values.get("rows") or values.get("schedule") or values.get("entries") or values.get("data")
        values = wrapped_values if wrapped_values is not None else ([values] if any(key in values for key in ("day", "hari", "course", "mata_kuliah", "subject")) else [])
    if not isinstance(values, list):
        return fallback_parse_schedule_entries(response_text)
    entries = []
    for value in values:
        if not isinstance(value, dict):
            continue
        day = _coerce_schedule_day(value.get("day") or value.get("hari") or "")
        start = str(value.get("start") or value.get("jam_mulai") or "").strip()
        end = str(value.get("end") or value.get("jam_selesai") or "").strip()
        time_range = str(value.get("time") or value.get("jam") or value.get("time_range") or value.get("jam_kuliah") or "").strip()
        if not time_range and not end and any(separator in start for separator in ("-", "–", "—")):
            time_range, start = start, ""
        if not start or not end:
            range_match = re.search(r"(\d{1,2})\s*[.:]\s*(\d{2})\s*(?:-|–|—|sampai|s/d)\s*(\d{1,2})\s*[.:]\s*(\d{2})", time_range, flags=re.IGNORECASE)
            if range_match:
                start = f"{range_match.group(1)}:{range_match.group(2)}"
                end = f"{range_match.group(3)}:{range_match.group(4)}"
        start = re.sub(r"[^0-9:]", "", start.replace(".", ":"))
        end = re.sub(r"[^0-9:]", "", end.replace(".", ":"))
        course = str(value.get("course") or value.get("course_name") or value.get("mata_kuliah") or value.get("subject") or "").strip()
        if re.fullmatch(r"\d{1,2}:\d{2}", start):
            start = start.zfill(5)
        if re.fullmatch(r"\d{1,2}:\d{2}", end):
            end = end.zfill(5)
        if day not in DAY_ORDER or not re.fullmatch(r"\d{2}:\d{2}", start) or not re.fullmatch(r"\d{2}:\d{2}", end) or not course:
            continue
        entries.append({"day": day, "start": start, "end": end, "course": course,
                        "room": str(value.get("room") or value.get("ruang") or "").strip(),
                        "lecturer": str(value.get("lecturer") or value.get("dosen") or "").strip(),
                        "class": str(value.get("class") or value.get("kelas") or "").strip()})
    return entries if entries else fallback_parse_schedule_entries(response_text)


def render_dashboard(request: Request, last_result: str | None = None, error: str | None = None,
                     active_result_id: int | None = None, result_title: str | None = None):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    context = make_dashboard_context(request, user_id, last_result, error, active_result_id, result_title)
    if context is None:
        response = RedirectResponse(url="/", status_code=303)
        response.delete_cookie("user_session")
        return response
    context["active_page"] = "dashboard"
    return templates.TemplateResponse(request=request, name="dashboard.html", context=context)


def render_ai_tools(request: Request, last_result: str | None = None, error: str | None = None,
                    active_result_id: int | None = None, result_title: str | None = None):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    context = make_dashboard_context(request, user_id, last_result, error, active_result_id, result_title)
    if context is None:
        response = RedirectResponse(url="/", status_code=303)
        response.delete_cookie("user_session")
        return response
    context["active_page"] = "ai"
    return templates.TemplateResponse(request=request, name="ai_tools.html", context=context)


def render_deadlines(request: Request, error: str | None = None, editing_task: dict | None = None):
    """Render the dedicated deadline workspace for the signed-in user."""
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    conn = get_db()
    user = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    tasks = [dict(row) for row in conn.execute(
        "SELECT * FROM deadlines WHERE user_id = ? ORDER BY status ASC, deadline_date ASC", (user_id,)
    ).fetchall()]
    conn.close()
    if not user:
        response = RedirectResponse(url="/", status_code=303)
        response.delete_cookie("user_session")
        return response
    for task in tasks:
        task["date_value"], task["time_value"] = deadline_fields(task["deadline_date"])
    return templates.TemplateResponse(
        request=request,
        name="deadlines.html",
        context={"request": request, "user_name": user["name"], "tasks": tasks,
                 "timezone_options": TIMEZONE_OPTIONS, "error": error, "editing_task": editing_task, "active_page": "tasks"},
    )


@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    if current_user_id(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="index.html", context={"form_data": {}})


@app.get("/register", response_class=HTMLResponse)
async def read_register(request: Request):
    if current_user_id(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="register.html", context={"form_data": {}})


@app.post("/register")
async def register(request: Request, name: str = Form(...), email: str = Form(...), institution: str = Form(...),
                   student_id: str | None = Form(None), major: str = Form(...), telegram_chat_id: str | None = Form(None),
                   password: str = Form(...), password_confirmation: str = Form(...)):
    form_data = {"name": name.strip(), "email": email.strip().lower(), "institution": institution.strip(),
                 "student_id": (student_id or "").strip(), "major": major.strip(), "telegram_chat_id": (telegram_chat_id or "").strip()}
    if not all((form_data["name"], form_data["email"], form_data["institution"], form_data["major"])):
        return auth_form_error("register.html", request, "Lengkapi semua kolom wajib sebelum mendaftar.", form_data)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", form_data["email"]):
        return auth_form_error("register.html", request, "Masukkan alamat email yang valid.", form_data)
    if form_data["telegram_chat_id"] and not re.fullmatch(r"-?\d+", form_data["telegram_chat_id"]):
        return auth_form_error("register.html", request, "ID Telegram harus berupa angka. Contoh: 123456789.", form_data)
    if len(password) < 8:
        return auth_form_error("register.html", request, "Kata sandi minimal terdiri dari 8 karakter.", form_data)
    if password != password_confirmation:
        return auth_form_error("register.html", request, "Konfirmasi kata sandi tidak sama.", form_data)
    conn = get_db()
    try:
        cursor = conn.cursor()
        exists = cursor.execute("SELECT 1 FROM users WHERE email = ?", (form_data["email"],)).fetchone()
        if exists:
            return auth_form_error("register.html", request, "Email tersebut sudah terdaftar. Silakan masuk.", form_data)
        cursor.execute(
            "INSERT INTO users (name, email, institution, student_id, major, telegram_chat_id, password_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (form_data["name"], form_data["email"], form_data["institution"], form_data["student_id"] or None,
             form_data["major"], form_data["telegram_chat_id"], hash_password(password)),
        )
        conn.commit()
        user_id = cursor.lastrowid
    finally:
        conn.close()
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie("user_session", session_value(user_id), httponly=True, samesite="lax")
    return response


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    conn = get_db()
    try:
        user = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    finally:
        conn.close()
    if not user or not password_matches(password, user["password_hash"]):
        return auth_form_error("index.html", request, "Email atau kata sandi salah.", {"email": email})
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie("user_session", session_value(user["id"]), httponly=True, samesite="lax")
    return response


@app.post("/start")
async def legacy_start():
    """Prevent old forms from bypassing account registration."""
    return RedirectResponse(url="/register", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("user_session")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def read_dashboard(request: Request):
    return render_dashboard(request)


@app.get("/ai-tools", response_class=HTMLResponse)
async def read_ai_tools(request: Request):
    return render_ai_tools(request)


def render_schedules(request: Request, error: str | None = None):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    conn = get_db()
    user = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    schedules = [dict(row) for row in conn.execute(
        "SELECT * FROM schedules WHERE user_id = ? ORDER BY id DESC", (user_id,)
    ).fetchall()]
    for schedule in schedules:
        entries = [dict(row) for row in conn.execute(
            "SELECT * FROM schedule_entries WHERE schedule_id = ? ORDER BY start_time, end_time", (schedule["id"],)
        ).fetchall()]
        slots = sorted({f"{entry['start_time']}–{entry['end_time']}" for entry in entries})
        days = sorted({entry["day_name"] for entry in entries}, key=lambda day: DAY_ORDER.get(day, 99))
        matrix: dict[str, dict[str, list[dict]]] = {day: {slot: [] for slot in slots} for day in days}
        for entry in entries:
            matrix[entry["day_name"]][f"{entry['start_time']}–{entry['end_time']}"] .append(entry)
        schedule["entries"], schedule["slots"], schedule["days"], schedule["matrix"] = entries, slots, days, matrix
    conn.close()
    return templates.TemplateResponse(request=request, name="schedules.html", context={"schedules": schedules, "user_name": user["name"] if user else "Mahasiswa", "active_page": "schedules", "error": error})


@app.get("/schedules", response_class=HTMLResponse)
async def read_schedules(request: Request):
    return render_schedules(request)


@app.post("/schedules/upload", response_class=HTMLResponse)
async def upload_schedule(request: Request, file: UploadFile = File(...)):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if suffix not in SCHEDULE_EXTENSIONS:
        return render_schedules(request, error="Jadwal harus berupa screenshot PNG/JPG, PDF, atau DOCX.")
    saved_path = UPLOAD_DIR / f"{uuid4().hex}{suffix}"
    try:
        with saved_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        response_text = extract_schedule_from_image(str(saved_path)) if suffix in {".png", ".jpg", ".jpeg"} else extract_schedule_entries(extract_text_from_file(str(saved_path)))
        entries = parse_schedule_entries(response_text)
    except Exception as exc:
        print(f"Schedule processing failed: {exc}")
        entries = []
    finally:
        await file.close()
        if saved_path.exists():
            saved_path.unlink()
    if not entries:
        return render_schedules(request, error="Jadwal tidak dapat dibaca sebagai tabel. Pastikan gambar tidak buram dan kolom Hari, Jam, serta Mata Kuliah terlihat jelas, lalu coba lagi.")
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO schedules (user_id, filename, schedule_text) VALUES (?, ?, ?)", (user_id, filename, "Jadwal terstruktur"))
        schedule_id = cursor.lastrowid
        cursor.executemany(
            "INSERT INTO schedule_entries (schedule_id, day_name, start_time, end_time, course_name, room, lecturer, class_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(schedule_id, entry["day"], entry["start"], entry["end"], entry["course"], entry["room"] or None,
              entry["lecturer"] or None, entry["class"] or None) for entry in entries],
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/schedules", status_code=303)


@app.post("/schedules/{schedule_id}/delete")
async def delete_schedule(request: Request, schedule_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM schedule_entries WHERE schedule_id IN (SELECT id FROM schedules WHERE id = ? AND user_id = ?)",
            (schedule_id, user_id),
        )
        conn.execute("DELETE FROM schedules WHERE id = ? AND user_id = ?", (schedule_id, user_id))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/schedules", status_code=303)


@app.get("/scholarships", response_class=HTMLResponse)
async def read_scholarships(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    conn = get_db()
    user = conn.execute("SELECT name, major FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    major = (user["major"] if user and user["major"] else "").strip()
    return templates.TemplateResponse(
        request=request, name="scholarships.html",
        context={"major": major, "scholarships": trusted_scholarships_for(major) if major else [], "user_name": user["name"] if user else "Mahasiswa", "active_page": "scholarships"},
    )


@app.post("/scholarships", response_class=HTMLResponse)
async def find_scholarships(request: Request, major: str = Form(...)):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    major = major.strip()
    if not major:
        return templates.TemplateResponse(request=request, name="scholarships.html", context={"major": "", "scholarships": [], "error": "Masukkan jurusan Anda.", "user_name": "Mahasiswa", "active_page": "scholarships"}, status_code=422)
    conn = get_db()
    conn.execute("UPDATE users SET major = ? WHERE id = ?", (major, user_id))
    conn.commit()
    user = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return templates.TemplateResponse(request=request, name="scholarships.html", context={"major": major, "scholarships": trusted_scholarships_for(major), "user_name": user["name"] if user else "Mahasiswa", "active_page": "scholarships"})


@app.post("/upload", response_class=HTMLResponse)
async def process_upload(request: Request, file: UploadFile = File(...), action_type: str = Form(...)):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return render_ai_tools(request, error="Format file tidak didukung. Gunakan PDF, DOCX, atau PPTX.")
    if action_type not in {"summary", "speaker", "formula"}:
        return render_ai_tools(request, error="Aksi agent tidak valid.")

    saved_path = UPLOAD_DIR / f"{uuid4().hex}{suffix}"
    try:
        with saved_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        extracted_text = extract_text_from_file(str(saved_path))
    finally:
        await file.close()
        if saved_path.exists():
            saved_path.unlink()
    if not extracted_text:
        return render_ai_tools(request, error="Teks dokumen tidak dapat dibaca. Pastikan file tidak terkunci dan berisi teks.")

    conn = get_db()
    cursor = conn.cursor()
    try:
        if action_type == "summary":
            result_text, label = generate_summary(extracted_text), "Rangkuman"
        elif action_type == "speaker":
            result_text, label = generate_speaker_notes(extracted_text), "Naskah Presentasi"
        elif action_type == "formula":
            result_text, label = generate_formula_explanation(extracted_text), "Bedah Rumus"
        elif action_type == "deadline":  # Kept for legacy records; the form no longer submits this action.
            entries = []
            for item in extract_deadlines(extracted_text):
                title = str(item.get("title", "Tugas tanpa judul")).strip() or "Tugas tanpa judul"
                deadline = str(item.get("deadline", "")).strip()
                if deadline:
                    cursor.execute("INSERT INTO deadlines (user_id, tugas_name, deadline_date) VALUES (?, ?, ?)", (user_id, title, deadline))
                    entries.append(f"• {title} — {deadline}")
            result_text, label = ("Deadline berhasil disimpan:\n\n" + "\n".join(entries), None) if entries else ("Tidak ditemukan deadline yang dapat disimpan dari dokumen ini.", None)
        if label:
            cursor.execute("INSERT INTO summaries (user_id, filename, action_type, result_text) VALUES (?, ?, ?, ?)", (user_id, filename, label, result_text))
            result_id = cursor.lastrowid
        conn.commit()
    except Exception as exc:
        print(f"Error processing document: {exc}")
        conn.rollback()
        return render_ai_tools(request, error="Dokumen gagal diproses oleh layanan AI. Periksa GEMINI_API_KEY dan model pada file .env, lalu coba lagi.")
    finally:
        conn.close()
    return render_ai_tools(request, last_result=result_text, active_result_id=result_id, result_title=label)


@app.post("/deadlines/extract-legacy-disabled", response_class=HTMLResponse)
async def process_deadline_text(request: Request, deadline_text: str = Form(...)):
    """Extract deadlines from pasted announcements, independently of document tools."""
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    deadline_text = deadline_text.strip()
    if not deadline_text:
        return render_dashboard(request, error="Tempelkan pengumuman atau daftar tugas terlebih dahulu.")

    conn = get_db()
    entries: list[str] = []
    try:
        for item in extract_deadlines(deadline_text):
            title = str(item.get("title", "Tugas tanpa judul")).strip() or "Tugas tanpa judul"
            deadline = str(item.get("deadline", "")).strip()
            if deadline:
                conn.execute(
                    "INSERT INTO deadlines (user_id, tugas_name, deadline_date) VALUES (?, ?, ?)",
                    (user_id, title, deadline),
                )
                entries.append(f"- **{title}** — {deadline}")
        conn.commit()
    except Exception as exc:
        print(f"Error extracting deadlines: {exc}")
        conn.rollback()
        return render_dashboard(request, error="Deadline gagal diekstrak. Pastikan layanan AI sudah dikonfigurasi, lalu coba lagi.")
    finally:
        conn.close()

    message = "## Tugas tersimpan\n" + "\n".join(entries) if entries else "## Tidak ada tugas yang bisa disimpan\nPastikan teks memuat nama tugas dan tanggal/jam pengumpulan yang jelas."
    return render_dashboard(request, last_result=message)


@app.get("/deadlines", response_class=HTMLResponse)
async def read_deadlines(request: Request):
    return render_deadlines(request)


@app.post("/deadlines", response_class=HTMLResponse)
async def create_deadline(request: Request, tugas_name: str = Form(...), deadline_date: str = Form(...),
                          deadline_time: str = Form(...), timezone_name: str = Form("Asia/Jakarta")):
    """Create a deadline from the three explicit inputs on the dashboard."""
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    tugas_name = tugas_name.strip()
    if not tugas_name:
        return render_dashboard(request, error="Judul tugas wajib diisi.")
    try:
        due_at = combine_deadline_input(deadline_date, deadline_time)
    except ValueError:
        return render_dashboard(request, error="Tanggal atau jam deadline tidak valid. Gunakan jam 24 jam, misalnya 13:30.")
    if timezone_name not in TIMEZONE_OPTIONS:
        return render_dashboard(request, error="Zona waktu tidak didukung.")
    if not is_future_deadline(due_at, timezone_name):
        return render_dashboard(request, error="Deadline harus berada di masa depan.")

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO deadlines (user_id, tugas_name, deadline_date, timezone) VALUES (?, ?, ?, ?)",
            (user_id, tugas_name, due_at.strftime("%Y-%m-%d %H:%M"), timezone_name),
        )
        conn.commit()
        has_telegram = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND telegram_chat_id IS NOT NULL AND TRIM(telegram_chat_id) <> ''",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    note = "" if has_telegram else "\n\n> Tambahkan Telegram Chat ID saat membuat akun agar notifikasi dapat dikirim."
    return render_dashboard(request, last_result=f"## Tugas tersimpan\n- **{tugas_name}**\n- Dikumpulkan: {due_at.strftime('%d-%m-%Y pukul %H:%M')} ({TIMEZONE_OPTIONS[timezone_name]})" + note)


@app.get("/results/{result_id}", response_class=HTMLResponse)
async def read_saved_result(request: Request, result_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    conn = get_db()
    row = conn.execute("SELECT * FROM summaries WHERE id = ? AND user_id = ?", (result_id, user_id)).fetchone()
    conn.close()
    if not row:
        return render_ai_tools(request, error="Hasil tersimpan tidak ditemukan.")
    return render_ai_tools(request, last_result=row["result_text"], active_result_id=row["id"], result_title=row["action_type"])


@app.get("/results/{result_id}/translate", response_class=HTMLResponse)
async def translate_saved_result(request: Request, result_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    conn = get_db()
    row = conn.execute("SELECT * FROM summaries WHERE id = ? AND user_id = ?", (result_id, user_id)).fetchone()
    conn.close()
    if not row:
        return render_ai_tools(request, error="Hasil tersimpan tidak ditemukan.")
    try:
        translated = translate_to_english(row["result_text"])
    except Exception as exc:
        print(f"Translation failed: {exc}")
        return render_ai_tools(request, error="Terjemahan gagal dibuat. Periksa konfigurasi layanan AI lalu coba lagi.")
    return render_ai_tools(
        request,
        last_result=translated,
        active_result_id=row["id"],
        result_title=f"English - {row['action_type']}",
    )


@app.get("/deadlines/{deadline_id}/edit", response_class=HTMLResponse)
async def edit_deadline_form(request: Request, deadline_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    conn = get_db()
    task = conn.execute("SELECT * FROM deadlines WHERE id = ? AND user_id = ?", (deadline_id, user_id)).fetchone()
    conn.close()
    if not task:
        return render_deadlines(request, error="Deadline tidak ditemukan.")
    task_data = dict(task)
    task_data["date_value"], task_data["time_value"] = deadline_fields(task_data["deadline_date"])
    return render_deadlines(request, editing_task=task_data)


@app.post("/deadlines/{deadline_id}/edit", response_class=HTMLResponse)
async def update_deadline(
    request: Request,
    deadline_id: int,
    tugas_name: str = Form(...),
    deadline_date: str = Form(...),
    deadline_time: str = Form(...),
    timezone_name: str = Form("Asia/Jakarta"),
):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    tugas_name = tugas_name.strip()
    try:
        due_at = combine_deadline_input(deadline_date, deadline_time)
    except ValueError:
        return render_deadlines(request, error="Tanggal atau jam tidak valid. Gunakan jam 24 jam, misalnya 13:30.")
    if timezone_name not in TIMEZONE_OPTIONS or not tugas_name or not is_future_deadline(due_at, timezone_name):
        return render_deadlines(request, error="Judul wajib diisi dan deadline harus berada di masa depan.")
    conn = get_db()
    updated = conn.execute(
        "UPDATE deadlines SET tugas_name = ?, deadline_date = ?, timezone = ? WHERE id = ? AND user_id = ?",
        (tugas_name, due_at.strftime("%Y-%m-%d %H:%M"), timezone_name, deadline_id, user_id),
    ).rowcount
    if updated:
        # A changed deadline deserves a fresh H-3/H-1/10-minute notification schedule.
        conn.execute("DELETE FROM deadline_notifications WHERE deadline_id = ?", (deadline_id,))
    conn.commit()
    conn.close()
    if not updated:
        return render_deadlines(request, error="Deadline tidak ditemukan.")
    return RedirectResponse(url="/deadlines", status_code=303)


@app.post("/deadlines/{deadline_id}/toggle")
async def toggle_deadline(request: Request, deadline_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    conn = get_db()
    conn.execute("UPDATE deadlines SET status = CASE WHEN status = 'pending' THEN 'completed' ELSE 'pending' END WHERE id = ? AND user_id = ?", (deadline_id, user_id))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/deadlines", status_code=303)


@app.post("/deadlines/{deadline_id}/delete")
async def delete_deadline(request: Request, deadline_id: int):
    user_id = current_user_id(request)
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    conn = get_db()
    conn.execute(
        "DELETE FROM deadline_notifications WHERE deadline_id IN (SELECT id FROM deadlines WHERE id = ? AND user_id = ?)",
        (deadline_id, user_id),
    )
    conn.execute("DELETE FROM deadlines WHERE id = ? AND user_id = ?", (deadline_id, user_id))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/deadlines", status_code=303)
