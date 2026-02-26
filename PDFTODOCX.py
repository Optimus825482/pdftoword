import os
import re
import uuid
import shutil
import logging
import tempfile
import struct
import zlib
import threading
from datetime import datetime
from flask import (
    Flask,
    request,
    send_file,
    render_template_string,
    abort,
    jsonify,
    Response,
)
from pdf2docx import Converter

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 35 * 1024 * 1024

APP_NAME = "NebulaDOC X"
APP_SHORT_NAME = "NDX"
OUTPUT_FILENAME_PREFIX = "nebuladocx"
ALLOWED_EXTENSIONS = {".pdf"}
APP_VERSION = "1.1.0"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
TEMP_DIR = os.path.join(BASE_DIR, "tmp")
COUNTER_FILE = os.path.join(BASE_DIR, "conversion_count.txt")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s"
)
logger = logging.getLogger("nebuladocx")
counter_lock = threading.Lock()


def _read_conversion_count_unlocked() -> int:
    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as file:
            raw_value = file.read().strip()
            return int(raw_value) if raw_value else 0
    except (FileNotFoundError, ValueError, OSError):
        return 0


def get_conversion_count() -> int:
    with counter_lock:
        return _read_conversion_count_unlocked()


def increment_conversion_count() -> int:
    with counter_lock:
        new_count = _read_conversion_count_unlocked() + 1
        with open(COUNTER_FILE, "w", encoding="utf-8") as file:
            file.write(str(new_count))
        return new_count


def sanitize_name(name: str) -> str:
    if not name:
        return "converted"
    name = name.replace("\\", "/").strip()
    name = os.path.basename(name)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    name = re.sub(r"_+", "_", name).strip("._")
    if not name:
        return "converted"
    return name


def safe_unlink(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        logger.warning("Geçici dosya silinemedi: %s", path)


def is_allowed_pdf(filename: str, mimetype: str | None) -> bool:
    extension = os.path.splitext((filename or "").lower())[1]
    if extension not in ALLOWED_EXTENSIONS:
        return False
    if not mimetype:
        return True
    return mimetype in {"application/pdf", "application/x-pdf"}


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    length = struct.pack("!I", len(data))
    crc = struct.pack("!I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    return length + chunk_type + data + crc


def generate_solid_png(size: int, rgb: tuple[int, int, int]) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    width = size
    height = size

    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)

    row = bytes([rgb[0], rgb[1], rgb[2]]) * width
    raw = b"".join(b"\x00" + row for _ in range(height))
    compressed = zlib.compress(raw, level=9)

    return (
        signature
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


PWA_ICON_192 = generate_solid_png(192, (11, 18, 48))
PWA_ICON_512 = generate_solid_png(512, (11, 18, 48))


SERVICE_WORKER_JS = f"""
const CACHE_NAME = "nebuladocx-cache-v{APP_VERSION}";
const APP_SHELL = [
  "/",
  "/manifest.webmanifest",
  "/pwa-icon.svg"
];

self.addEventListener("install", (event) => {{
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
}});

self.addEventListener("activate", (event) => {{
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
    ))
  );
  self.clients.claim();
}});

self.addEventListener("fetch", (event) => {{
  const req = event.request;
  if (req.method !== "GET") return;

  if (req.mode === "navigate") {{
    event.respondWith(
      fetch(req).catch(() => caches.match("/"))
    );
    return;
  }}

  const sameOrigin = new URL(req.url).origin === self.location.origin;
  if (sameOrigin) {{
    event.respondWith(
      caches.match(req).then((cached) => {{
        const fetched = fetch(req)
          .then((networkRes) => {{
            const copy = networkRes.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
            return networkRes;
          }})
          .catch(() => cached);
        return cached || fetched;
      }})
    );
  }}
}});
"""


PWA_ICON_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0B1230"/>
      <stop offset="100%" stop-color="#1D1B4B"/>
    </linearGradient>
    <linearGradient id="edge" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#8FA8FF"/>
      <stop offset="100%" stop-color="#4AF2FF"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="110" fill="url(#bg)"/>
  <rect x="92" y="92" width="328" height="328" rx="58" fill="none" stroke="url(#edge)" stroke-width="18"/>
  <rect x="128" y="128" width="256" height="256" rx="34" fill="none" stroke="#DCE4FF" stroke-opacity="0.55" stroke-width="8"/>
  <text x="256" y="288" text-anchor="middle" font-family="Arial, sans-serif" font-size="88" letter-spacing="10" font-weight="700" fill="#EEF4FF">NDX</text>
</svg>
"""


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <meta name="description" content="NebulaDOC X ile PDF dosyalarını hızlı, güvenli ve profesyonel şekilde DOCX formatına dönüştürün.">
    <meta name="theme-color" content="#0B1230">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="NebulaDOC X">
    <link rel="manifest" href="/manifest.webmanifest">
    <link rel="icon" href="/pwa-icon.svg" type="image/svg+xml">
    <link rel="apple-touch-icon" href="/pwa-icon.svg">
    <title>NebulaDOC X | PDF → DOCX</title>
    <script src="https://unpkg.com/@tailwindcss/browser@4"></script>
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-0: #050816;
            --bg-1: #0d1333;
            --panel: rgba(16, 24, 56, 0.68);
            --line: rgba(141, 166, 255, 0.35);
            --line-strong: rgba(156, 184, 255, 0.75);
            --text: #ecf2ff;
            --muted: #b6c2ee;
            --accent-1: #7c8cff;
            --accent-2: #37d7ff;
            --accent-3: #b07cff;
            --ok: #2be6a0;
            --warn: #ffd166;
        }
        * { box-sizing: border-box; }
        body {
            font-family: 'Space Grotesk', sans-serif;
            background: radial-gradient(circle at 15% 20%, #182452 0%, transparent 30%),
                        radial-gradient(circle at 82% 18%, #301c5e 0%, transparent 35%),
                        radial-gradient(circle at 50% 100%, #0f1a40 0%, transparent 40%),
                        linear-gradient(160deg, var(--bg-0), var(--bg-1));
            color: var(--text);
            min-height: 100dvh;
        }
        .noise {
            position: fixed;
            inset: 0;
            pointer-events: none;
            background-image: radial-gradient(rgba(255,255,255,0.08) 0.6px, transparent 0.6px);
            background-size: 3px 3px;
            opacity: .08;
        }
        .grid-lines {
            position: fixed;
            inset: 0;
            pointer-events: none;
            background-image:
                linear-gradient(rgba(129, 156, 255, 0.10) 1px, transparent 1px),
                linear-gradient(90deg, rgba(129, 156, 255, 0.10) 1px, transparent 1px);
            background-size: 42px 42px;
            mask-image: radial-gradient(circle at 50% 50%, black 45%, transparent 95%);
        }
        .orb {
            position: fixed;
            width: 28rem;
            height: 28rem;
            border-radius: 999px;
            filter: blur(65px);
            opacity: 0.28;
            pointer-events: none;
            transition: transform 0.12s linear;
        }
        .orb-a { background: #4e6dff; top: -8rem; right: -8rem; }
        .orb-b { background: #2ee6ff; bottom: -10rem; left: -10rem; }
        .frame {
            background: var(--panel);
            border: 1px solid var(--line);
            box-shadow: 0 24px 80px rgba(8, 14, 34, .70), inset 0 0 0 1px rgba(188, 203, 255, .07);
            backdrop-filter: blur(14px);
        }
        .brand-ring {
            width: 74px;
            height: 74px;
            border-radius: 22px;
            border: 1.5px solid var(--line-strong);
            display: grid;
            place-items: center;
            position: relative;
            background: linear-gradient(150deg, rgba(124,140,255,.22), rgba(55,215,255,.12));
            box-shadow: 0 0 0 1px rgba(180,197,255,.12), 0 0 30px rgba(80,116,255,.25);
        }
        .brand-ring::before {
            content: "";
            position: absolute;
            inset: 8px;
            border: 1px solid rgba(176, 196, 255, .45);
            border-radius: 14px;
        }
        .logo-mark {
            font-size: 1.05rem;
            font-weight: 700;
            letter-spacing: .08em;
            color: #eef2ff;
        }
        .chip {
            border: 1px solid rgba(168, 188, 255, .35);
            background: rgba(123, 144, 226, .12);
            color: var(--muted);
        }
        .drop-zone {
            border: 1px dashed rgba(165, 183, 255, 0.45);
            background: rgba(120, 140, 220, 0.08);
            transition: all .25s ease;
        }
        .drop-zone.active {
            border-color: rgba(87, 214, 255, .95);
            box-shadow: 0 0 0 1px rgba(87, 214, 255, .35), 0 0 35px rgba(87, 214, 255, .18);
            background: rgba(71, 153, 255, .13);
        }
        .primary-btn {
            background: linear-gradient(135deg, var(--accent-1), var(--accent-2));
            box-shadow: 0 10px 30px rgba(84, 118, 255, 0.35);
            color: #071326;
        }
        .primary-btn:hover {
            filter: brightness(1.05);
            transform: translateY(-1px);
        }
        .download-btn {
            background: linear-gradient(135deg, var(--accent-3), var(--accent-2));
            color: #071326;
        }
        .install-btn {
            border: 1px solid rgba(137, 178, 255, .45);
            background: rgba(116, 138, 255, .14);
        }
        .net-pill {
            border: 1px solid rgba(153, 184, 255, 0.3);
            background: rgba(84, 113, 255, 0.15);
            color: #d7e4ff;
        }
        .net-pill.offline {
            border-color: rgba(255, 209, 102, 0.55);
            background: rgba(255, 209, 102, 0.12);
            color: #ffe9bc;
        }
        .wait-wrap {
            display: none;
            align-items: center;
            gap: .55rem;
            margin-top: .65rem;
            color: #d9e7ff;
            font-size: .9rem;
        }
        .wait-wrap.show {
            display: inline-flex;
        }
        .spinner {
            width: 16px;
            height: 16px;
            border-radius: 999px;
            border: 2px solid rgba(157, 191, 255, .35);
            border-top-color: #8fd8ff;
            animation: spin .75s linear infinite;
        }
        .dots::after {
            content: '';
            display: inline-block;
            width: 1.2em;
            text-align: left;
            animation: dots 1.2s steps(4, end) infinite;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        @keyframes dots {
            0% { content: ''; }
            25% { content: '.'; }
            50% { content: '..'; }
            75% { content: '...'; }
            100% { content: ''; }
        }
    </style>
</head>
<body class="overflow-x-hidden relative px-4 py-8 md:py-10">
    <div class="noise"></div>
    <div class="grid-lines"></div>
    <div class="orb orb-a"></div>
    <div class="orb orb-b"></div>

    <main class="frame rounded-3xl w-full max-w-3xl p-5 md:p-10 relative z-10 mx-auto">
        <header class="flex flex-col gap-4 md:gap-6">
            <div class="flex items-start justify-between gap-3">
                <div class="flex items-center gap-4">
                    <div class="brand-ring" aria-hidden="true">
                        <span class="logo-mark">NDX</span>
                    </div>
                    <div>
                        <p class="uppercase tracking-[0.20em] text-[11px] text-indigo-200/90">Bordered Identity</p>
                        <h1 class="text-2xl md:text-4xl font-bold leading-tight">NebulaDOC X</h1>
                        <p class="text-sm text-indigo-100/80 mt-1">PDF → DOCX dönüşümünde yüksek doğruluk, modern hız.</p>
                    </div>
                </div>
                <span id="networkState" class="net-pill text-xs px-3 py-1.5 rounded-full">Online</span>
            </div>

            <div class="flex flex-wrap gap-2">
                <span class="chip text-xs px-3 py-1 rounded-full">PWA Enabled</span>
                <span class="chip text-xs px-3 py-1 rounded-full">Secure Upload</span>
                <span class="chip text-xs px-3 py-1 rounded-full">35MB Limit</span>
                <span class="chip text-xs px-3 py-1 rounded-full">Production Ready</span>
            </div>
        </header>

        <section class="mt-7 md:mt-8">
            <form id="uploadForm" class="space-y-4" enctype="multipart/form-data" novalidate>
                <label for="pdfFile" id="dropZone" class="drop-zone rounded-2xl p-5 md:p-7 cursor-pointer block">
                    <div class="flex flex-col md:flex-row md:items-center gap-4">
                        <div class="size-12 rounded-xl border border-indigo-200/30 bg-indigo-200/10 flex items-center justify-center shrink-0">
                            <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" class="text-cyan-200">
                                <path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/>
                            </svg>
                        </div>
                        <div class="flex-1 min-w-0">
                            <p class="font-semibold text-base md:text-lg">PDF dosyasını sürükle-bırak veya seç</p>
                            <p id="fileHint" class="text-sm text-indigo-100/70 truncate">Yalnızca .pdf • maksimum 35MB • dönüştürme sonrası anında indirme</p>
                        </div>
                        <span class="text-xs md:text-sm border border-indigo-200/35 rounded-lg px-3 py-2 text-indigo-50/90">Dosya Seç</span>
                    </div>
                    <input type="file" name="pdf" id="pdfFile" accept=".pdf,application/pdf" class="sr-only" required>
                </label>

                <div class="flex flex-col sm:flex-row gap-3">
                    <button id="convertBtn" type="submit" class="primary-btn transition-all duration-200 flex-1 font-semibold py-3 rounded-xl">
                        Convert to DOCX
                    </button>
                    <button id="installBtn" type="button" class="install-btn text-white font-medium py-3 px-5 rounded-xl">
                        Uygulama Olarak Yükle
                    </button>
                </div>
            </form>

            <div id="progressContainer" class="mt-5 w-full h-3 rounded-full overflow-hidden bg-indigo-100/10 hidden" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
                <div id="progressBar" class="h-full text-[10px] leading-3 font-semibold text-black text-center bg-gradient-to-r from-green-300 to-cyan-300 transition-all duration-300" style="width:0%">0%</div>
            </div>

            <p id="status" class="mt-3 text-sm text-indigo-100/80" aria-live="polite"></p>

            <a id="downloadBtn" aria-disabled="true" class="download-btn mt-4 opacity-45 pointer-events-none w-full md:w-auto justify-center inline-flex items-center px-5 py-3 font-semibold rounded-xl shadow-md" href="#" target="_blank" download>
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" class="mr-2">
                    <path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/>
                </svg>
                DOCX Dosyasını İndir
            </a>

            <div id="waitingWrap" class="wait-wrap" aria-live="polite">
                <span class="spinner" aria-hidden="true"></span>
                <span class="dots">Lütfen bekleyin</span>
            </div>
        </section>

        <footer class="mt-8 pt-5 border-t border-indigo-200/20 text-center space-y-1">
            <p class="text-xs text-indigo-100/90">© 2026 Code by Erkan Erdem</p>
            <p class="text-xs text-indigo-100/80">Kullanım tamamen ücretsizdir ve ücretsiz kalacaktır.</p>
            <p class="text-xs text-indigo-100/80">Bugüne kadar başarıyla çevrilen dosya: <span id="conversionCounter" class="font-semibold text-cyan-200">{{ conversion_count }}</span></p>
        </footer>
    </main>

    <script>
        const orbs = document.querySelectorAll('.orb');
        document.addEventListener('mousemove', (event) => {
            const x = event.clientX / window.innerWidth;
            const y = event.clientY / window.innerHeight;
            orbs.forEach((orb, index) => {
                const power = index === 0 ? 22 : -18;
                orb.style.transform = `translate(${(x - 0.5) * power}px, ${(y - 0.5) * power}px)`;
            });
        });

        const form = document.getElementById('uploadForm');
        const status = document.getElementById('status');
        const progressContainer = document.getElementById('progressContainer');
        const progressBar = document.getElementById('progressBar');
        const downloadBtn = document.getElementById('downloadBtn');
        const waitingWrap = document.getElementById('waitingWrap');
        const fileInput = document.getElementById('pdfFile');
        const dropZone = document.getElementById('dropZone');
        const fileHint = document.getElementById('fileHint');
        const convertBtn = document.getElementById('convertBtn');
        const installBtn = document.getElementById('installBtn');
        const networkState = document.getElementById('networkState');
        const conversionCounter = document.getElementById('conversionCounter');

        let deferredPrompt = null;
        let isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;

        function getInstallHelpMessage() {
            const ua = navigator.userAgent || '';
            const isIOS = /iPhone|iPad|iPod/i.test(ua);
            const isAndroid = /Android/i.test(ua);
            const isEdgeOrChromeDesktop = /Edg|Chrome/i.test(ua) && !isAndroid;

            if (!window.isSecureContext) {
                return 'PWA kurulumu için site HTTPS üzerinde açılmalıdır.';
            }

            if (isIOS) {
                return 'iPhone/iPad için Safari menüsünden Paylaş → Ana Ekrana Ekle yolunu kullanın.';
            }
            if (isAndroid) {
                return 'Android için tarayıcı menüsünden “Uygulamayı yükle” veya “Ana ekrana ekle” seçin.';
            }
            if (isEdgeOrChromeDesktop) {
                return 'Adres çubuğundaki yükle simgesini (install) kullanarak uygulamayı kurabilirsiniz.';
            }
            return 'Bu tarayıcı otomatik yükleme penceresi göstermeyebilir. Tarayıcı menüsünden “Uygulamayı yükle/Ana ekrana ekle” seçeneğini kullanın.';
        }

        function syncInstallButtonState() {
            if (isStandalone) {
                installBtn.textContent = 'Uygulama Yüklü';
                installBtn.disabled = true;
                installBtn.classList.add('opacity-60', 'cursor-not-allowed');
            }
        }

        function updateNetworkBadge() {
            if (navigator.onLine) {
                networkState.textContent = 'Online';
                networkState.classList.remove('offline');
            } else {
                networkState.textContent = 'Offline (yalnızca önbellek)';
                networkState.classList.add('offline');
            }
        }

        function setSelectedFile(file) {
            if (!file) return;
            const sizeMB = (file.size / (1024 * 1024)).toFixed(2);
            fileHint.textContent = `${file.name} • ${sizeMB} MB`;
            localStorage.setItem('ndx:last-file-name', file.name);
        }

        const rememberedFileName = localStorage.getItem('ndx:last-file-name');
        if (rememberedFileName) {
            fileHint.textContent = `Son seçilen: ${rememberedFileName}`;
        }

        ['dragenter', 'dragover'].forEach((eventName) => {
            dropZone.addEventListener(eventName, (event) => {
                event.preventDefault();
                dropZone.classList.add('active');
            });
        });

        ['dragleave', 'drop'].forEach((eventName) => {
            dropZone.addEventListener(eventName, (event) => {
                event.preventDefault();
                dropZone.classList.remove('active');
            });
        });

        dropZone.addEventListener('drop', (event) => {
            const files = event.dataTransfer.files;
            if (files && files.length > 0) {
                fileInput.files = files;
                setSelectedFile(files[0]);
            }
        });

        fileInput.addEventListener('change', () => {
            if (fileInput.files && fileInput.files.length > 0) {
                setSelectedFile(fileInput.files[0]);
            }
        });

        function readErrorFromBlob(blob) {
            return new Promise((resolve) => {
                if (!blob) {
                    resolve('Bilinmeyen bir hata oluştu.');
                    return;
                }
                const reader = new FileReader();
                reader.onload = () => {
                    try {
                        const parsed = JSON.parse(String(reader.result));
                        resolve(parsed.error || parsed.message || 'Dönüştürme başarısız oldu.');
                    } catch {
                        resolve(String(reader.result || 'Dönüştürme başarısız oldu.'));
                    }
                };
                reader.onerror = () => resolve('Dönüştürme başarısız oldu.');
                reader.readAsText(blob);
            });
        }

        form.addEventListener('submit', function (event) {
            event.preventDefault();

            if (!fileInput.files || fileInput.files.length === 0) {
                status.textContent = 'Lütfen önce bir PDF dosyası seçin.';
                return;
            }

            status.textContent = '';
            waitingWrap.classList.add('show');
            downloadBtn.setAttribute('aria-disabled', 'true');
            downloadBtn.classList.add('opacity-45', 'pointer-events-none');
            progressBar.style.width = '0%';
            progressBar.textContent = '0%';
            progressContainer.classList.remove('hidden');
            progressContainer.setAttribute('aria-valuenow', '0');
            convertBtn.disabled = true;
            convertBtn.classList.add('opacity-70', 'cursor-not-allowed');
            status.textContent = 'Dosya dönüştürülüyor...';

            const formData = new FormData(form);
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/convert', true);
            xhr.responseType = 'blob';

            xhr.upload.addEventListener('progress', function (e) {
                if (e.lengthComputable) {
                    const pct = Math.round((e.loaded / e.total) * 100);
                    progressBar.style.width = pct + '%';
                    progressBar.textContent = pct + '% (yükleme)';
                    progressContainer.setAttribute('aria-valuenow', String(pct));
                }
            });

            xhr.addEventListener('progress', function (e) {
                if (e.lengthComputable) {
                    const pct = Math.round((e.loaded / e.total) * 100);
                    progressBar.style.width = pct + '%';
                    progressBar.textContent = pct + '% (indirme)';
                    progressContainer.setAttribute('aria-valuenow', String(pct));
                }
            });

            xhr.onload = async function () {
                if (this.status === 200) {
                    const blob = this.response;
                    const disposition = xhr.getResponseHeader('Content-Disposition');
                    let filename = 'converted.docx';
                    if (disposition && disposition.includes('filename=')) {
                        filename = disposition.split('filename=')[1].replace(/"/g, '');
                    }
                    const url = window.URL.createObjectURL(blob);
                    downloadBtn.href = url;
                    downloadBtn.download = filename;
                    downloadBtn.setAttribute('aria-disabled', 'false');
                    downloadBtn.classList.remove('opacity-45', 'pointer-events-none');
                    status.textContent = 'Dönüştürme tamamlandı. Dosyanız hazır.';
                    if (conversionCounter) {
                        const current = Number.parseInt(conversionCounter.textContent || '0', 10);
                        if (!Number.isNaN(current)) {
                            conversionCounter.textContent = String(current + 1);
                        }
                    }
                } else {
                    const errMsg = await readErrorFromBlob(this.response);
                    status.textContent = 'Hata: ' + errMsg;
                }
                waitingWrap.classList.remove('show');
                progressContainer.classList.add('hidden');
                convertBtn.disabled = false;
                convertBtn.classList.remove('opacity-70', 'cursor-not-allowed');
            };

            xhr.onerror = function () {
                status.textContent = 'Ağ hatası oluştu. Tekrar deneyin.';
                waitingWrap.classList.remove('show');
                progressContainer.classList.add('hidden');
                convertBtn.disabled = false;
                convertBtn.classList.remove('opacity-70', 'cursor-not-allowed');
            };

            xhr.send(formData);
        });

        window.addEventListener('beforeinstallprompt', (event) => {
            event.preventDefault();
            deferredPrompt = event;
            if (!isStandalone) {
                installBtn.textContent = 'Uygulama Olarak Yükle';
                installBtn.disabled = false;
                installBtn.classList.remove('opacity-60', 'cursor-not-allowed');
            }
        });

        installBtn.addEventListener('click', async () => {
            if (isStandalone) {
                status.textContent = 'Uygulama zaten yüklü.';
                return;
            }

            if (!deferredPrompt) {
                status.textContent = getInstallHelpMessage();
                return;
            }

            deferredPrompt.prompt();
            await deferredPrompt.userChoice;
            deferredPrompt = null;
        });

        window.addEventListener('appinstalled', () => {
            isStandalone = true;
            syncInstallButtonState();
            status.textContent = 'NebulaDOC X cihazınıza yüklendi.';
        });

        if ('serviceWorker' in navigator) {
            window.addEventListener('load', () => {
                navigator.serviceWorker.register('/service-worker.js').catch(() => {
                    console.warn('Service worker kayıt edilemedi.');
                });
            });
        }

        window.addEventListener('online', updateNetworkBadge);
        window.addEventListener('offline', updateNetworkBadge);
        updateNetworkBadge();
        syncInstallButtonState();
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        HTML_TEMPLATE,
        conversion_count=get_conversion_count(),
    )


@app.route("/manifest.webmanifest")
def web_manifest():
    manifest_payload = {
        "name": APP_NAME,
        "short_name": APP_SHORT_NAME,
        "description": "PDF dosyalarını hızlı ve güvenli şekilde DOCX formatına dönüştürün.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#0B1230",
        "theme_color": "#0B1230",
        "lang": "tr",
        "icons": [
            {
                "src": "/pwa-icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/pwa-icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/pwa-icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            },
        ],
    }
    response = jsonify(manifest_payload)
    response.mimetype = "application/manifest+json"
    return response


@app.route("/service-worker.js")
def service_worker():
    response = Response(SERVICE_WORKER_JS, mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/pwa-icon.svg")
def pwa_icon():
    return Response(PWA_ICON_SVG, mimetype="image/svg+xml")


@app.route("/pwa-icon-192.png")
def pwa_icon_192():
    return Response(PWA_ICON_192, mimetype="image/png")


@app.route("/pwa-icon-512.png")
def pwa_icon_512():
    return Response(PWA_ICON_512, mimetype="image/png")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": APP_NAME, "version": APP_VERSION})


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


@app.errorhandler(400)
def handle_400(err):
    return jsonify({"error": getattr(err, "description", "Geçersiz istek.")}), 400


@app.errorhandler(413)
def handle_413(_):
    return (
        jsonify({"error": "Dosya boyutu çok büyük. Maksimum 35MB desteklenir."}),
        413,
    )


@app.errorhandler(500)
def handle_500(err):
    return jsonify({"error": getattr(err, "description", "Sunucu hatası oluştu.")}), 500


@app.route("/convert", methods=["POST"])
def convert():
    if "pdf" not in request.files:
        abort(400, description="PDF dosyası gönderilmedi.")

    pdf_file = request.files["pdf"]
    filename = (pdf_file.filename or "").strip()
    if filename == "":
        abort(400, description="Seçili PDF dosyası yok.")
    if not is_allowed_pdf(filename, pdf_file.mimetype):
        abort(400, description="Yalnızca PDF dosyası yükleyebilirsiniz.")

    source_stem = sanitize_name(os.path.splitext(filename)[0])
    unique_suffix = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    output_name = (
        sanitize_name(f"{OUTPUT_FILENAME_PREFIX}_{source_stem}_{unique_suffix}")
        + ".docx"
    )
    final_path = os.path.join(OUTPUT_DIR, output_name)

    temp_pdf_path = ""
    temp_docx_path = ""
    converter = None

    try:
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".pdf", dir=TEMP_DIR
        ) as temp_pdf:
            pdf_file.save(temp_pdf)
            temp_pdf_path = temp_pdf.name

        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".docx", dir=TEMP_DIR
        ) as temp_docx:
            temp_docx_path = temp_docx.name

        converter = Converter(temp_pdf_path)
        converter.convert(temp_docx_path, start=0)

        shutil.move(temp_docx_path, final_path)
        temp_docx_path = ""

        increment_conversion_count()

        response = send_file(
            final_path, as_attachment=True, download_name=output_name, max_age=0
        )
        logger.info("Dönüştürme başarılı: %s", output_name)
        return response
    except Exception as exc:
        logger.exception("Dönüştürme hatası")
        abort(500, description=f"Dönüştürme sırasında hata oluştu: {exc}")
    finally:
        if converter is not None:
            try:
                converter.close()
            except Exception:
                logger.warning("Converter kapanışı sırasında hata oluştu.")
        safe_unlink(temp_pdf_path)
        safe_unlink(temp_docx_path)


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
