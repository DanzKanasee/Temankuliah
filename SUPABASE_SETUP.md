# Supabase database

1. Buat project di Supabase.
2. Buka **SQL Editor**, jalankan seluruh isi `supabase_schema.sql`.
3. Salin connection string PostgreSQL dari **Project Settings > Database > Connection string**.
4. Tambahkan connection string tersebut ke `.env` sebagai `SUPABASE_DB_URL`.
5. Pasang dependency dengan `pip install -r requirements.txt`, lalu jalankan aplikasi.

Saat `SUPABASE_DB_URL` tersedia, aplikasi menggunakan database PostgreSQL Supabase. Tanpa variabel tersebut, aplikasi tetap menggunakan SQLite lokal untuk development.

## Migrasi data lama

Schema ini membuat tabel kosong. Data dari `app_mahasiswa.db` tidak berpindah otomatis. Export data SQLite terlebih dahulu, kemudian import ke tabel Supabase dengan urutan `users`, `deadlines`, `summaries`, `schedules`, `schedule_entries`, dan `deadline_notifications`.

## Vercel

Tambahkan `SUPABASE_DB_URL`, `TELEGRAM_TOKEN`, `GEMINI_API_KEY`, dan `CRON_SECRET` di **Project Settings > Environment Variables**. Jangan commit `.env` atau connection string ke repository.

Vercel Cron memanggil `/api/cron/reminders` setiap menit. Endpoint ini mengirim pengingat untuk semua pengguna yang memiliki deadline pending dan menggunakan `CRON_SECRET` untuk membatasi akses langsung.