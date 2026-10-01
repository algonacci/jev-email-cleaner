# jev-email-cleaner

Memproses seluruh inbox Gmail melalui IMAP dan meminta Orvix Jev mengklasifikasikannya dalam halaman Flask/Jinja.

## Menjalankan

1. Aktifkan 2-Step Verification di akun Google dan buat App Password.
2. Salin `.env.example` menjadi `.env`, lalu isi email dan App Password Gmail.
3. Jalankan aplikasi:

```bash
uv run flask --app app run --debug
```

Buka `http://127.0.0.1:5000`, lalu tekan tombol untuk melanjutkan klasifikasi seluruh Inbox. Setiap email dikirim sebagai satu request Orvix dan hasilnya langsung disimpan ke SQLite sebagai `keep`, `review`, atau `delete`. Fase klasifikasi tidak mengubah email Gmail. Aksi Gmail dijalankan pada fase terpisah.

Hasil dan keputusan disimpan di `instance/email_cleaner.db`. Pengambilan email menggunakan inbox read-only dan tidak menandai email sebagai sudah dibaca. Tombol Move to trash memindahkan email ke Trash Gmail, tidak menghapusnya secara permanen.

Kebijakan klasifikasi mempertahankan bukti transaksi, invoice, receipt, pajak, perbankan, keamanan, akses, sertifikat, expiry yang membutuhkan tindakan, korespondensi, dan peluang relevan. Notifikasi rutin tanpa tindakan atau nilai arsip diarahkan ke `delete`, tetapi tidak dipindahkan secara otomatis.
