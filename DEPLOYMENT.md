# NebulaDOC X - Deployment Notes

## 0) Proje Özeti

- Framework: Flask + `pdf2docx`
- Runtime: Python 3.12
- Production server: Waitress
- PWA: `manifest.webmanifest` + `service-worker.js` + install prompt
- Container deploy: Dockerfile + `docker-compose.yml` + Caddy reverse proxy
- GitHub repository: https://github.com/Optimus825482/pdftoword

## 1) Kurulum

```powershell
cd D:\PLYGRND\pdftoword
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 2) Development Çalıştırma

```powershell
$env:FLASK_DEBUG="true"
python PDFTODOCX.py
```

## 3) Production Çalıştırma (Waitress)

```powershell
$env:FLASK_DEBUG="false"
python -m waitress --host=0.0.0.0 --port=5000 PDFTODOCX:app
```

Alternatif tek komut:

```powershell
.\run_prod.ps1
```

## 4) Sağlık Kontrolü

```powershell
Invoke-WebRequest http://127.0.0.1:5000/healthz
```

Beklenen cevap: JSON içinde `{"status":"ok","service":"NebulaDOC X","version":"..."}`

## 5) GitHub'a Push

```powershell
git init
git add .
git commit -m "feat: add PWA support and production deployment stack"
git branch -M main
git remote add origin <GITHUB_REPO_URL>
git push -u origin main
```

> Not: Eğer remote zaten varsa `git remote set-url origin <GITHUB_REPO_URL>` kullan.

## 6) Coolify ile Deploy

1. Coolify panelinde **New Resource → Application → Public Repository** seç.
2. GitHub repo URL'sini gir.
3. Build Pack yerine **Dockerfile** kullan.
4. Port: `5000`
5. Healthcheck path: `/healthz`
6. Deploy branch: `main`
7. Deploy başlat ve logları kontrol et.

## 6.1) Docker Compose ile Domain Deploy (`pdf.erkanerdem.net`)

Bu proje artık Caddy reverse proxy ile `docker-compose.yml` üzerinden doğrudan domaine yayın yapar.

### Sunucu Hazırlığı

- DNS A kaydı: `pdf.erkanerdem.net` → sunucu public IP
- Sunucuda `80` ve `443` portları açık olmalı
- Docker + Docker Compose kurulu olmalı

### Çalıştırma

```bash
cp .env.example .env
# gerekirse EMAIL değerini düzenle
docker compose up -d --build
```

### Kontrol

```bash
docker compose ps
docker compose logs -f caddy
docker compose logs -f app
```

Domain üzerinden healthcheck:

```bash
curl -fsSL https://pdf.erkanerdem.net/healthz
```

> Caddy otomatik olarak Let's Encrypt TLS sertifikasını alır/yeniler.

## 6.2) Coolify'da Compose Kullanımı

Coolify tarafında istersen Dockerfile yerine Compose ile deploy edebilirsin:

1. New Resource → Application → Public Repository
2. Repo: `https://github.com/Optimus825482/pdftoword`
3. Deployment Type: **Docker Compose**
4. Compose file: `docker-compose.coolify.yml`
5. Coolify Domain ayarına: `https://pdf.erkanerdem.net` gir
6. Port: `5000`, Healthcheck: `/healthz`
7. Deploy ve sağlık kontrolünü `https://pdf.erkanerdem.net/healthz` ile doğrula

> Not: Coolify kendi proxy/TLS katmanını yönettiği için burada Caddy içeren `docker-compose.yml` yerine `docker-compose.coolify.yml` kullanılır.

## 7) PWA Doğrulama Checklist

- Browser DevTools → Application sekmesinde:
	- Manifest görülüyor olmalı
	- Service Worker active olmalı
	- Icon ve theme-color yüklenmeli
- Mobil cihazda “Ana ekrana ekle”/Install prompt görünmeli
- Uygulama install sonrası standalone açılmalı

## 8) Production Önerileri

- Uygulamayı reverse proxy (Nginx/Caddy/IIS) arkasında çalıştır.
- `output` klasörü için düzenli temizlik (scheduled task) ekle.
- TLS/HTTPS zorunlu kullan.
- Logları dosya veya merkezi log sistemine yönlendir.
- Büyük dosya/sık istek için rate limit eklemeyi değerlendir.
- Coolify tarafında auto-deploy'u sadece `main` ve korumalı branch stratejisiyle kullan.
