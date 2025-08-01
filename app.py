# app.py - Versión mejorada con reescritura completa de HLS

import os
import re
import time
import threading
import requests
from flask import Flask, Response, request, abort, url_for
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

app = Flask(__name__)

BASE_URL = "https://la14hd.com/vivo/canales.php?stream={}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://la14hd.com/"
}

STREAM_CACHE = {}
CACHE_TTL = 300
LOCK = threading.Lock()

def load_channels():
    channels = []
    try:
        with open("canales.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    channels.append(line)
    except FileNotFoundError:
        print("⚠️ canales.txt no encontrado")
    return channels

CHANNELS = load_channels()


def extract_m3u8_url(canal):
    url = BASE_URL.format(canal)
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(url, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')
        scripts = soup.find_all('script')

        for script in scripts:
            if script.string:
                # Regex corregido y seguro
                match = re.search(r'https?://[^\s\'"\\<>]+\.m3u8\?token=[^\s\'"\\<>]+', script.string)
                if match:
                    return match.group(0)
    except Exception as e:
        print(f"❌ Error extrayendo m3u8 para {canal}: {e}")
    return None


def rewrite_m3u8(content, base_url, canal):
    """Reescribe el .m3u8 para que todos los recursos pasen por el proxy"""
    lines = content.splitlines()
    rewritten = []

    for line in lines:
        stripped = line.strip()

        # Reescribir URLs de segmentos (.ts), claves (.key), etc.
        if stripped.startswith("http"):
            parsed = urlparse(stripped)
            token = re.search(r'token=([^&]+)', parsed.query)
            token_val = token.group(1) if token else ""

            # Codificar la URL real para pasársela al proxy
            proxy_segment_url = url_for('proxy_segment', canal=canal, real_url=stripped, _external=True)
            rewritten.append(proxy_segment_url)

        elif stripped.endswith(".ts") or ".m3u8" in stripped or ".key" in stripped:
            # URL relativa
            abs_url = urljoin(base_url, stripped)
            proxy_segment_url = url_for('proxy_segment', canal=canal, real_url=abs_url, _external=True)
            rewritten.append(proxy_segment_url)
        else:
            rewritten.append(line)  # línea original (comentarios, headers)

    return "\n".join(rewritten)


@app.route('/stream/<canal>.m3u8')
def proxy_playlist(canal):
    if canal not in CHANNELS:
        abort(404)

    cached = STREAM_CACHE.get(canal)
    if not cached or time.time() > cached.get('expires', 0):
        with LOCK:
            # Refrescar solo si es necesario
            m3u8_url = extract_m3u8_url(canal)
            if m3u8_url:
                STREAM_CACHE[canal] = {
                    'm3u8_url': m3u8_url,
                    'base_url': '/'.join(m3u8_url.split('/')[:-1]) + '/',
                    'expires': time.time() + CACHE_TTL
                }
            else:
                abort(500, "No se pudo obtener el stream")

    cache_info = STREAM_CACHE[canal]
    m3u8_url = cache_info['m3u8_url']

    try:
        r = requests.get(m3u8_url, headers={**HEADERS, "Referer": "https://la14hd.com/"}, timeout=10)
        r.raise_for_status()

        # Reescribir el contenido del .m3u8
        content = r.text
        rewritten_content = rewrite_m3u8(content, cache_info['base_url'], canal)

        # Devolver el .m3u8 reescrito
        return Response(rewritten_content, mimetype="application/x-mpegurl")

    except Exception as e:
        print(f"❌ Error al obtener .m3u8: {e}")
        abort(502)


@app.route('/proxy/segment/<canal>')
def proxy_segment(canal):
    if canal not in CHANNELS:
        abort(404)

    real_url = request.args.get("real_url")
    if not real_url:
        abort(400)

    try:
        # Descargar el recurso (ts, key, m3u8 secundario)
        r = requests.get(real_url, headers=HEADERS, stream=True, timeout=10)
        r.raise_for_status()

        def generate():
            for chunk in r.iter_content(chunk_size=1024):
                yield chunk

        headers = {k: v for k, v in r.headers.items() if k.lower() not in ['content-length', 'transfer-encoding']}
        return Response(generate(), status=r.status_code, headers=headers)

    except Exception as e:
        print(f"❌ Error proxyeando segmento: {e}")
        abort(502)


@app.route('/m3u')
def generate_m3u():
    base = request.host_url.rstrip("/")
    lines = ["#EXTM3U"]

    for canal in CHANNELS:
        lines.append(f'#EXTINF:-1,{canal.title()}\n{base}/stream/{canal}.m3u8')

    return Response("\n".join(lines), mimetype="application/x-mpegurl")


@app.route('/')
def home():
    links = '<h1>📡 Proxy HLS de La14HD</h1><ul>'
    for canal in CHANNELS:
        links += f'<li><a href="/stream/{canal}.m3u8">{canal}</a></li>'
    links += f'</ul><p><a href="/m3u">📥 Descargar M3U</a></p>'
    return links


# Refresco en segundo plano
def background_refresh():
    while True:
        time.sleep(240)
        for canal in CHANNELS:
            if canal not in STREAM_CACHE or time.time() > STREAM_CACHE[canal].get('expires', 0):
                threading.Thread(target=extract_m3u8_url, args=(canal,), daemon=True).start()

threading.Thread(target=background_refresh, daemon=True).start()


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
