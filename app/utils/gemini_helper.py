import os
import json
import re
import mimetypes
from pathlib import Path
from datetime import datetime
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# Inisialisasi Google GenAI Client
api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key) if api_key else None
# Dapat dioverride melalui .env, misalnya GEMINI_MODEL=gemini-3.6-flash.
DEFAULT_GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-2.5-pro",
]


def get_model_name() -> str:
    configured_model = os.getenv("GEMINI_MODEL", "").strip()
    if configured_model:
        return configured_model
    return DEFAULT_GEMINI_MODELS[0]


MODEL_NAME = get_model_name()


def generate_text(prompt: str, max_output_tokens: int) -> str:
    """Jalur cepat untuk tugas dokumen yang tidak membutuhkan reasoning panjang."""
    if not client:
        return ""
    last_error = None
    for candidate_model in [os.getenv("GEMINI_MODEL", "").strip() or MODEL_NAME] + [
        model for model in DEFAULT_GEMINI_MODELS if model != (os.getenv("GEMINI_MODEL", "").strip() or MODEL_NAME)
    ]:
        try:
            response = client.models.generate_content(
                model=candidate_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=max_output_tokens,
                    thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                ),
            )
            return response.text or ""
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return ""


def format_schedule(text: str) -> str:
    """Turn extracted schedule text into a consistent, storable Indonesian schedule."""
    if not client:
        return ""
    prompt = f"""
Kamu menata jadwal kuliah dari dokumen pengguna. Tulis HANYA jadwal yang benar-benar terbaca.
Gunakan Markdown sederhana: judul '# Jadwal Kuliah', lalu kelompokkan berdasarkan hari.
Setiap mata kuliah harus berupa bullet dengan format '**Jam** — Mata kuliah | kelas/ruang | dosen' bila informasinya tersedia.
Jangan mengarang hari, jam, ruang, atau dosen. Jika teks tidak cukup jelas, tulis bagian '## Perlu dikonfirmasi'.

Teks dokumen:
{text[:12000]}
"""
    return generate_text(prompt, max_output_tokens=1800)


def _extract_schedule_from_media(file_path: str, mime_type: str) -> str:
    """Read a schedule image or scanned PDF directly into structured rows."""
    if not client and mime_type.startswith("image/"):
        try:
            from PIL import Image
            import pytesseract
        except Exception:
            return ""
        try:
            image = Image.open(file_path)
            text = pytesseract.image_to_string(image, lang="eng+ind")
            image.close()
            return text.strip()
        except Exception as exc:
            print(f"OCR fallback failed for {file_path}: {exc}")
            return ""
    if not client:
        return ""
    path = Path(file_path)
    prompt = """Baca tabel jadwal kuliah pada gambar dengan teliti dari baris pertama sampai terakhir.
Kolom yang mungkin ada: Kode, Mata Kuliah, Kelas, Hari, Jam, Ruang, Dosen.
Kembalikan HANYA array JSON valid, tanpa Markdown, tanpa penjelasan. Satu baris tabel menjadi satu objek:
{"day":"SENIN","start":"07:30","end":"09:10","course":"Nama Mata Kuliah","room":"B314","lecturer":"Nama Dosen","class":"2RPBO-A"}
Normalisasi hari ke SENIN, SELASA, RABU, KAMIS, JUMAT, SABTU, atau MINGGU. JUM'AT, JUMAT, dan JUM harus selalu ditulis sebagai JUMAT.
Normalisasi jam ke HH:MM. Contoh 07.30-09.10 menjadi start 07:30 dan end 09:10.
Jangan melewatkan baris hanya karena Kode, ruang, dosen, atau kelas kosong. Gunakan string kosong untuk kolom yang tidak terbaca.
Jangan mengarang dan jangan menambahkan jadwal yang tidak ada di dokumen. Pastikan setiap baris yang memiliki Hari, termasuk baris JUMAT, ikut dikembalikan. Dokumen dapat berupa tabel hasil scan atau diputar 90 derajat; sesuaikan orientasi saat membacanya."""
    media_part = types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type)
    configured_model = os.getenv("GEMINI_MODEL", "").strip() or MODEL_NAME
    candidates = [configured_model] + [model for model in DEFAULT_GEMINI_MODELS if model != configured_model]
    last_error = None
    day_names = {"SENIN", "SELASA", "RABU", "KAMIS", "JUMAT", "JUM'AT", "JUM", "MINGGU", "SABTU",
                 "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"}

    def has_schedule_row(value: object) -> bool:
        if isinstance(value, dict):
            rows = value.get("rows") or value.get("schedule") or value.get("entries") or value.get("data")
            if rows is None:
                rows = [value]
            elif isinstance(rows, dict):
                rows = [rows]
        else:
            rows = value
        if not isinstance(rows, list):
            return False
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = str(row.get("day") or row.get("hari") or "").upper().strip()
            normalized_row = {str(key).lower().strip(): item for key, item in row.items()}
            course = normalized_row.get("course") or normalized_row.get("course_name") or normalized_row.get("mata_kuliah") or normalized_row.get("mata kuliah") or normalized_row.get("subject")
            time_value = " ".join(str(normalized_row.get(key) or "") for key in ("start", "end", "time", "jam", "jam_mulai", "jam_selesai", "time_range", "jam kuliah"))
            if day in day_names and course and re.search(r"\d{1,2}\s*[.:]\s*\d{2}", time_value):
                return True
        return False

    for candidate_model in candidates:
        try:
            response = client.models.generate_content(
                model=candidate_model,
                contents=[media_part, prompt],
                config=types.GenerateContentConfig(
                    max_output_tokens=4000,
                    response_mime_type="application/json",
                ),
            )
            result = response.text or ""
            if result.strip():
                cleaned = result.replace("```json", "").replace("```", "").strip()
                json_match = re.search(r"\[\s*\{.*\}\s*\]", cleaned, flags=re.DOTALL)
                try:
                    values = json.loads(json_match.group(0) if json_match else cleaned)
                    if has_schedule_row(values):
                        return result
                    last_error = ValueError("Gemini returned JSON without a complete schedule row")
                except (TypeError, ValueError):
                    last_error = ValueError("Gemini returned invalid schedule JSON")
        except Exception as exc:
            last_error = exc
            print(f"Gemini image parsing failed with {candidate_model}: {exc}")

    # Keep image uploads usable when Gemini is temporarily unavailable and a
    # local Tesseract installation is present (for example, on a local server).
    if not mime_type.startswith("image/"):
        return ""
    try:
        from PIL import Image
        import pytesseract

        with Image.open(file_path) as image:
            return pytesseract.image_to_string(image, lang="eng+ind").strip()
    except Exception as exc:
        if last_error:
            print(f"Local schedule OCR fallback failed for {file_path}: {exc}")
        return ""


def extract_schedule_from_image(file_path: str) -> str:
    """Read a schedule screenshot directly into a JSON data set."""
    mime_type = mimetypes.guess_type(file_path)[0] or "image/png"
    return _extract_schedule_from_media(file_path, mime_type)


def extract_schedule_from_pdf(file_path: str) -> str:
    """Read a scanned schedule PDF directly when it has no extractable text."""
    return _extract_schedule_from_media(file_path, "application/pdf")


def extract_schedule_entries(text: str) -> str:
    """Extract schedule rows from text originating in a PDF or Word document."""
    if not client:
        return text or ""
    prompt = f"""Ekstrak SEMUA baris jadwal kuliah dari teks berikut. Kembalikan HANYA JSON valid array.
Format setiap objek: {{"day":"SENIN","start":"07:30","end":"09:10","course":"Nama Mata Kuliah","room":"B314","lecturer":"Nama Dosen","class":"Kelas"}}.
Jam wajib HH:MM, hari wajib SENIN/SELASA/RABU/KAMIS/JUMAT/SABTU/MINGGU. Jangan menebak nilai yang tidak tertulis; pakai string kosong untuk room, lecturer, atau class yang tidak ada.
Teks jadwal:\n{text[:18000]}"""
    try:
        return generate_text(prompt, max_output_tokens=4000)
    except Exception as exc:
        print(f"Gemini schedule parsing failed: {exc}")
        return text or ""

def generate_summary(text: str) -> str:
    """Fitur 1: Membuat rangkuman materi perkuliahan."""
    if not client:
        return "API Key Gemini belum dikonfigurasi di file .env."
    
    prompt = f"""
    Kamu adalah asisten dosen dan akademik yang sangat ahli.
    Buat rangkuman yang ringkas, rapi, dan mudah dipahami dari teks materi berikut.
    Gunakan Markdown yang benar dan konsisten agar nyaman dibaca di aplikasi:
    - Awali dengan judul "# Ringkasan: [judul materi]".
    - Beri bagian "## Inti materi", lalu maksimal 7 poin pendek berbentuk bullet.
    - Tambahkan "## Istilah penting" (jika ada) dan "## Yang perlu diingat" berisi 2-3 poin.
    - Gunakan **tebal** hanya untuk kata kunci; jangan memakai simbol LaTex, panah kode, atau tabel.
    - Tulis dalam Bahasa Indonesia yang sederhana dan jangan mengulang pembuka atau isi materi.

    Teks Materi:
    {text[:6000]}
    """
    return generate_text(prompt, max_output_tokens=1000)

def extract_deadlines(text: str) -> list:
    """Fitur 2: Menganalisis dan mengekstrak daftar deadline tugas ke format JSON."""
    if not client:
        return []

    prompt = f"""
    Analisis teks berikut dan ekstrak semua daftar tugas, proyek, atau deadline ujian yang ditemukan.
    
    ATURAN SANGAT PENTING:
    1. Kembalikan HANYA format JSON valid tanpa tanda markdown (seperti ```json).
    2. Kolom "deadline" WAJIB diisi format tanggal "YYYY-MM-DD" atau "YYYY-MM-DD HH:MM".
    3. Hanya masukkan deadline dengan tanggal yang cukup jelas. Jangan menebak atau membuat tanggal baru.

    Format JSON yang wajib diikuti:
    [
        {{"title": "Nama Tugas/Ujian", "deadline": "YYYY-MM-DD 23:59"}}
    ]

    Teks:
    {text[:4000]}
    """
    response_text = generate_text(prompt, max_output_tokens=600)
    try:
        clean_json = response_text.replace('```json', '').replace('```', '').strip()
        data = json.loads(clean_json)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error parsing deadline JSON: {e}")
        return []

def generate_speaker_notes(text: str) -> str:
    """Fitur 3: Membuat naskah/skrip omongan presentasi slide demi slide."""
    if not client:
        return "API Key Gemini belum dikonfigurasi."

    prompt = f"""
    Kamu adalah pembicara/presenter profesional.
    Berdasarkan materi slide/dokumen berikut, buatkan Naskah Presentasi (Speaker Notes) yang siap dibaca slide demi slide.
    Sertakan salam pembuka, transisi antarslide, dan penutup.

    Materi Slide:
    {text[:7000]}
    """
    return generate_text(prompt, max_output_tokens=1400)

def generate_formula_explanation(text: str) -> str:
    """Explain formulas when present, otherwise explain the document's actual concepts."""
    if not client:
        return "API Key Gemini belum dikonfigurasi."

    prompt = f"""
    Kamu adalah tutor akademik yang teliti. Bedah isi materi HANYA berdasarkan
    informasi yang benar-benar tertulis pada dokumen, bukan berdasarkan asumsi bidang studi.

    Langkah wajib sebelum menulis:
    1. Tentukan apakah dokumen memuat rumus/perhitungan eksplisit atau hanya konsep/proses.
    2. Jika hanya konsep/proses, gunakan judul "# Bedah Konsep Materi". Jelaskan konsep,
       tahapan, hubungan antar gagasan, dan contoh penerapan yang relevan dengan materi.
       JANGAN menciptakan rumus, teori, singkatan, atau istilah teknis yang tidak ada di dokumen.
    3. Jika ada rumus eksplisit, gunakan judul "# Bedah Rumus Materi". Uraikan hanya rumus
       yang ada: arti variabel, cara membaca, kapan digunakan, serta satu contoh hitungan sederhana.

    Format keluaran wajib berupa Markdown sederhana:
    - Gunakan hanya heading # dan ##, bullet (-), serta **tebal** untuk istilah kunci.
    - Jangan gunakan LaTeX, tanda $, backslash, diagram formal, atau heading ###/####.
    - Awali dengan ## Gambaran materi, kemudian ## Pembahasan per konsep atau rumus,
      dan akhiri ## Inti yang perlu diingat.
    - Gunakan Bahasa Indonesia yang ringkas, runtut, dan mudah dipahami mahasiswa.

    Materi Dokumen:
    {text[:7000]}
    """
    return generate_text(prompt, max_output_tokens=1400)


def translate_to_english(text: str) -> str:
    """Translate a saved AI result while preserving its readable Markdown structure."""
    if not client:
        return "API Key Gemini belum dikonfigurasi."
    prompt = f"""
    Translate the following academic content into clear, natural English.
    Preserve headings, bullet points, bold Markdown, formulas, and overall structure.
    Return only the translated content, without commentary.

    Content:
    {text[:12000]}
    """
    return generate_text(prompt, max_output_tokens=1800)
