# AI Agent Mahasiswa Web

Aplikasi web berbasis FastAPI untuk membantu mahasiswa mengelola materi kuliah, deadline tugas, jadwal, dan informasi beasiswa. Aplikasi juga menyediakan fitur AI untuk memproses dokumen menggunakan Google Gemini serta pengingat deadline melalui Telegram.

## Fitur

- Registrasi, login, dan session berbasis cookie yang ditandatangani.
- Rangkuman dokumen PDF, DOCX, dan PPTX.
- Pembuatan naskah presentasi dan catatan pembicara.
- Bedah rumus serta penjelasan materi.
- Penerjemahan hasil AI ke bahasa Inggris.
- Ekstraksi deadline dari dokumen dan pengelolaan deadline secara manual.
- Unggah jadwal kuliah dari PDF, DOCX, atau gambar.
- Rekomendasi beasiswa dari katalog sumber resmi.
- Pengingat deadline melalui Telegram.
- Dukungan SQLite lokal dan PostgreSQL Supabase.

## Persyaratan

- Python 3.10 atau lebih baru
- API key Google Gemini untuk fitur AI
- Bot Telegram dan tokennya jika ingin memakai pengingat Telegram
- Akun Supabase jika ingin menggunakan database PostgreSQL

## Instalasi Lokal

### 1. Buat virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Pasang dependency

```bash
pip install -r requirements.txt
```

### 3. Atur environment variable

Buat file `.env` di root project:

```env
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-3.8-flash
SESSION_SECRET=ganti-dengan-secret-acak-yang-panjang

# Opsional: aktifkan penyimpanan dan pengingat Telegram
TELEGRAM_TOKEN=your_telegram_bot_token
APP_TIMEZONE=Asia/Jakarta

# Opsional: tanpa variabel ini aplikasi memakai SQLite lokal
SUPABASE_DB_URL=postgresql://user:password@host:5432/database

# Opsional: diperlukan untuk endpoint cron di deployment
CRON_SECRET=secret-untuk-cron
```

Jangan commit file `.env` atau API key ke repository.

### 4. Siapkan database

Untuk development, aplikasi otomatis membuat database SQLite `app_mahasiswa.db` saat dijalankan.

Untuk Supabase:

1. Buat project di Supabase.
2. Buka **SQL Editor**.
3. Jalankan seluruh isi [`supabase_schema.sql`](supabase_schema.sql).
4. Salin connection string PostgreSQL ke `SUPABASE_DB_URL` di `.env`.

Detail tambahan tersedia di [`SUPABASE_SETUP.md`](SUPABASE_SETUP.md).

### 5. Jalankan aplikasi

```bash
python run.py
```

Buka [http://127.0.0.1:8000](http://127.0.0.1:8000) di browser. Mode development menggunakan Uvicorn dengan auto-reload.

## Pengingat Telegram

Isi `TELEGRAM_TOKEN` dengan token bot Telegram. Pengguna harus menyimpan Telegram chat ID pada profil agar dapat menerima notifikasi. Dalam mode lokal, aplikasi menjalankan pemeriksaan pengingat secara berkala. Saat dideploy ke Vercel, pengingat dijalankan melalui endpoint:

```text
GET /api/cron/reminders
```

Jika `CRON_SECRET` diatur, request harus mengirim header berikut:

```text
Authorization: Bearer <CRON_SECRET>
```

## Deployment ke Vercel

Project sudah menyediakan [`vercel.json`](vercel.json) dan entry point [`api/index.py`](api/index.py). Tambahkan environment variable berikut pada **Project Settings > Environment Variables**:

- `GEMINI_API_KEY`
- `GEMINI_MODEL` (opsional)
- `SESSION_SECRET`
- `TELEGRAM_TOKEN` (opsional)
- `APP_TIMEZONE` (opsional)
- `SUPABASE_DB_URL` atau `DATABASE_URL`
- `CRON_SECRET` (opsional, tetapi disarankan)

Vercel Cron dapat memanggil `/api/cron/reminders` setiap menit. SQLite di lingkungan Vercel bersifat sementara, sehingga Supabase/PostgreSQL disarankan untuk deployment produksi.

## Struktur Project

```text
api/                  Entry point deployment Vercel
app/main.py           Aplikasi dan route FastAPI
app/database/         Koneksi serta inisialisasi database
app/services/         Logika deadline dan pengingat Telegram
app/utils/            Integrasi Gemini dan parser dokumen
app/templates/        Template HTML Jinja2
tests/                Pengujian
uploads/              File unggahan lokal
run.py                Launcher development
```

## Pengujian

Aktifkan virtual environment, lalu jalankan:

```bash
pytest
```
