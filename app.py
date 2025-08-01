# app.py
import os
import re
import time
import threading
import requests
from flask import Flask, Response, request, abort
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import logging

app = Flask(__name__)

# Configuración
BASE_URL = "https://la14hd.com/vivo/canales.php?stream={}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Almacenamiento en memoria (puedes usar Redis en producción)
STREAM_CACHE = {}
CACHE_TTL = 300  # 5 minutos de caché
LOCK = threading.Lock()

# Leer canales desde archivo
def load_channels():
    channels = []
    try:
        with open("canales.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    channels.append(line)
    except FileNotFoundError:
        print("⚠️ Archivo canales.txt no encontrado. Asegúrate de crearlo.")
    return channels

CHANNELS = load_channels()

def extract_m3u8_url(canal):
    """Extrae la URL .m3u8 real desde la página del canal"""
    url = BASE_URL.format(canal)
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        
        response = session.get(url, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')

        # Buscar script que contenga la URL del stream
        scripts = soup.find_all('script')
        for script in scripts:
            if script.string and '.m3u8' in script.string:
                # Extraer URL con regex
                match = re.search(r'https?://[^"\s\'\\]+\.m3u8[^"\s\'\\]*', script.string)
                if match:
                    m3u8_url = match.group(0)
                    return m3u8_url

        # Alternativa: buscar iframe o data-src
        iframe = soup.find('iframe', src=True)
        if iframe:
            iframe_url = urljoin(response.url, iframe['src'])
            # Aquí podrías hacer un segundo scraping si el stream está en otro dominio
            # Pero en este caso, asumimos que el .m3u8 está en el script principal

    except Exception as e:
        print(f"❌ Error extrayendo m3u8 para {canal}: {e}")
    return None

def refresh_stream(canal):
    """Refresca la URL del stream y la guarda en caché"""
    with LOCK:
        m3u8_url = extract_m3u8_url(canal)
        if m3u8_url:
            STREAM_CACHE[canal] = {
                'url': m3u8_url,
                'expires': time.time() + CACHE_TTL
            }
            print(f"✅ Stream actualizado para '{canal}': {m3u8_url}")
        else:
            print(f"❌ No se pudo obtener stream para '{canal}'")

@app.route('/stream/<canal>')
def proxy_stream(canal):
    """Endpoint que sirve como proxy inverso al stream real"""
    if canal not in CHANNELS:
        abort(404, "Canal no encontrado")

    # Verificar caché
    cached = STREAM_CACHE.get(canal)
    if not cached or time.time() > cached['expires']:
        threading.Thread(target=refresh_stream, args=(canal,), daemon=True).start()
        if not cached:
            refresh_stream(canal)
            cached = STREAM_CACHE.get(canal)
            if not cached:
                abort(500, "No se pudo cargar el stream")

    target_url = cached['url']

    # Hacer streaming de la respuesta del .m3u8
    try:
        r = requests.get(target_url, stream=True, headers=HEADERS, timeout=10)
        r.raise_for_status()

        # Devolver el contenido como stream
        def generate():
            for chunk in r.iter_content(chunk_size=1024):
                yield chunk

        headers = dict(r.headers)
        excluded_headers = ['content-length', 'connection', 'transfer-encoding']
        filtered_headers = {
            k: v for k, v in headers.items() if k.lower() not in excluded_headers
        }

        return Response(generate(), status=r.status_code, headers=filtered_headers)

    except Exception as e:
        print(f"❌ Error al proxyear stream {canal}: {e}")
        abort(502, "Error al conectar con el stream origen")

@app.route('/m3u')
def generate_m3u():
    """Genera una lista M3U con todos los canales del proxy"""
    base_host = request.host_url.rstrip("/")  # Ej: https://tuapp.onrender.com
    m3u_lines = ["#EXTM3U x-tvg-url=\"https://iptv-org.github.io/epg/guides/tvplus.com.epg.xml\""]

    for canal in CHANNELS:
        # Forzar actualización si no está en caché
        if canal not in STREAM_CACHE or time.time() > STREAM_CACHE[canal].get('expires', 0):
            refresh_stream(canal)

        stream_info = STREAM_CACHE.get(canal, {})
        tvg_name = canal.title()
        tvg_logo = f"{base_host}/static/logos/{canal}.png"  # Opcional: agregar logos
        group_title = "La14HD"

        m3u_line = (
            f'#EXTINF:-1 tvg-name="{tvg_name}" tvg-logo="{tvg_logo}" '
            f'group-title="{group_title}", {tvg_name}\n'
            f'{base_host}/stream/{canal}'
        )
        m3u_lines.append(m3u_line)

    return Response("\n".join(m3u_lines), mimetype="application/x-mpegurl")

@app.route('/')
def home():
    links = "<h2>Canales disponibles:</h2><ul>"
    for canal in CHANNELS:
        links += f'<li><a href="/stream/{canal}">{canal}</a> | ' \
                 f'<a href="{request.host_url}stream/{canal}" target="_blank">🔗 URL</a></li>'
    links += "</ul>"
    links += f'<p><a href="/m3u">📄 Descargar lista M3U</a></p>'
    return f"<h1>📡 Proxy Inverso de La14HD</h1>{links}"

if __name__ == '__main__':
    # Inicializar caché al inicio
    for canal in CHANNELS:
        refresh_stream(canal)

    # Refresco periódico cada 4 minutos (menos que TTL)
    def periodic_refresh():
        while True:
            time.sleep(240)
            for canal in CHANNELS:
                refresh_stream(canal)

    threading.Thread(target=periodic_refresh, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
