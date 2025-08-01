#!/usr/bin/env python3
"""
Proxy Inverso M3U8 para la14hd.com
Extrae URLs M3U8 y las sirve sin restricciones de IP
"""

import asyncio
import aiohttp
import re
import os
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs
from aiohttp import web
import json
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass
import hashlib

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class ChannelInfo:
    name: str
    stream_id: str
    m3u8_url: Optional[str] = None
    last_updated: Optional[float] = None
    status: str = "inactive"

class M3U8ProxyServer:
    def __init__(self):
        self.base_url = "https://la14hd.com/vivo/canales.php"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
        self.channels: Dict[str, ChannelInfo] = {}
        self.session: Optional[aiohttp.ClientSession] = None
        self.proxy_sessions: Dict[str, aiohttp.ClientSession] = {}
        self.cache_duration = 300  # 5 minutos
        
        # Headers para simular navegador real
        self.headers = {
            'User-Agent': self.user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0'
        }

    async def initialize(self):
        """Inicializar sesiones HTTP"""
        connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=30,
            ttl_dns_cache=300,
            use_dns_cache=True,
            ssl=False,
            enable_cleanup_closed=True
        )
        
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=self.headers
        )
        
        # Cargar canales desde archivo
        await self.load_channels_from_file()
        logger.info(f"Servidor inicializado con {len(self.channels)} canales")

    async def load_channels_from_file(self, filename: str = "canales.txt"):
        """Cargar lista de canales desde archivo de texto"""
        try:
            if os.path.exists(filename):
                with open(filename, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if line and not line.startswith('#'):
                            try:
                                # Formato: nombre_canal:stream_id
                                if ':' in line:
                                    name, stream_id = line.split(':', 1)
                                    name = name.strip()
                                    stream_id = stream_id.strip()
                                else:
                                    # Si no hay nombre, usar el stream_id como nombre
                                    stream_id = line
                                    name = stream_id
                                
                                self.channels[stream_id] = ChannelInfo(
                                    name=name,
                                    stream_id=stream_id
                                )
                                logger.info(f"Canal cargado: {name} ({stream_id})")
                            except Exception as e:
                                logger.error(f"Error procesando línea {line_num}: {line} - {e}")
            else:
                # Crear archivo de ejemplo
                example_channels = [
                    "# Formato: nombre_canal:stream_id",
                    "# Ejemplo:",
                    "Fox Sports:foxsports",
                    "ESPN:espn",
                    "CNN:cnn"
                ]
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(example_channels))
                logger.info(f"Archivo de ejemplo creado: {filename}")
                
        except Exception as e:
            logger.error(f"Error cargando canales: {e}")

    async def extract_m3u8_url(self, stream_id: str) -> Optional[str]:
        """Extraer URL M3U8 desde la página de la14hd"""
        try:
            url = f"{self.base_url}?stream={stream_id}"
            logger.info(f"Extrayendo M3U8 para {stream_id} desde {url}")
            
            async with self.session.get(url) as response:
                if response.status != 200:
                    logger.error(f"Error HTTP {response.status} para {stream_id}")
                    return None
                
                content = await response.text()
                
                # Buscar patrones de URL M3U8
                patterns = [
                    r'https://[^"\'>\s]+\.m3u8[^"\'>\s]*',
                    r'"(https://[^"]+\.m3u8[^"]*)"',
                    r"'(https://[^']+\.m3u8[^']*)'",
                    r'src="([^"]+\.m3u8[^"]*)"',
                    r"src='([^']+\.m3u8[^']*)'",
                    r'file:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
                ]
                
                for pattern in patterns:
                    matches = re.findall(pattern, content, re.IGNORECASE)
                    if matches:
                        m3u8_url = matches[0] if isinstance(matches[0], str) else matches[0][0]
                        logger.info(f"URL M3U8 encontrada para {stream_id}: {m3u8_url}")
                        return m3u8_url
                
                # Buscar en scripts JavaScript
                js_pattern = r'(?:source|src|file|url)[\s\'"]*[:=][\s\'"]*(["\']?)([^"\'>\s]+\.m3u8[^"\'>\s]*)\1'
                js_matches = re.findall(js_pattern, content, re.IGNORECASE)
                if js_matches:
                    m3u8_url = js_matches[0][1]
                    logger.info(f"URL M3U8 encontrada en JS para {stream_id}: {m3u8_url}")
                    return m3u8_url
                
                logger.warning(f"No se encontró URL M3U8 para {stream_id}")
                return None
                
        except Exception as e:
            logger.error(f"Error extrayendo M3U8 para {stream_id}: {e}")
            return None

    async def update_channel_url(self, stream_id: str):
        """Actualizar URL M3U8 de un canal específico"""
        if stream_id not in self.channels:
            return False
        
        channel = self.channels[stream_id]
        current_time = time.time()
        
        # Verificar si necesita actualización (cache)
        if (channel.last_updated and 
            current_time - channel.last_updated < self.cache_duration and 
            channel.m3u8_url):
            return True
        
        # Extraer nueva URL
        m3u8_url = await self.extract_m3u8_url(stream_id)
        if m3u8_url:
            channel.m3u8_url = m3u8_url
            channel.last_updated = current_time
            channel.status = "active"
            logger.info(f"Canal {stream_id} actualizado: {m3u8_url}")
            return True
        else:
            channel.status = "error"
            return False

    async def proxy_m3u8_content(self, original_url: str) -> Optional[bytes]:
        """Descargar y procesar contenido M3U8"""
        try:
            session_key = hashlib.md5(original_url.encode()).hexdigest()[:8]
            
            if session_key not in self.proxy_sessions:
                connector = aiohttp.TCPConnector(ssl=False)
                self.proxy_sessions[session_key] = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers=self.headers
                )
            
            session = self.proxy_sessions[session_key]
            
            async with session.get(original_url) as response:
                if response.status == 200:
                    content = await response.read()
                    logger.info(f"Contenido M3U8 descargado: {len(content)} bytes")
                    return content
                else:
                    logger.error(f"Error descargando M3U8: {response.status}")
                    return None
                    
        except Exception as e:
            logger.error(f"Error en proxy M3U8: {e}")
            return None

    async def handle_channel_stream(self, request):
        """Manejar solicitud de stream de canal específico"""
        stream_id = request.match_info.get('stream_id')
        
        if stream_id not in self.channels:
            return web.json_response(
                {"error": f"Canal {stream_id} no encontrado"}, 
                status=404
            )
        
        # Actualizar URL del canal
        success = await self.update_channel_url(stream_id)
        if not success:
            return web.json_response(
                {"error": f"No se pudo obtener stream para {stream_id}"}, 
                status=503
            )
        
        channel = self.channels[stream_id]
        
        # Descargar contenido M3U8
        content = await self.proxy_m3u8_content(channel.m3u8_url)
        if not content:
            return web.json_response(
                {"error": f"Error descargando stream {stream_id}"}, 
                status=502
            )
        
        return web.Response(
            body=content,
            content_type='application/vnd.apple.mpegurl',
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type',
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0'
            }
        )

    async def handle_playlist_m3u(self, request):
        """Generar playlist M3U con todos los canales"""
        try:
            host = request.headers.get('Host', 'localhost:8080')
            scheme = 'https' if 'render' in host else 'http'
            base_url = f"{scheme}://{host}"
            
            m3u_content = ["#EXTM3U"]
            
            for stream_id, channel in self.channels.items():
                channel_url = f"{base_url}/stream/{stream_id}"
                m3u_content.append(f"#EXTINF:-1,{channel.name}")
                m3u_content.append(channel_url)
            
            playlist = "\n".join(m3u_content)
            
            return web.Response(
                text=playlist,
                content_type='audio/x-mpegurl',
                headers={
                    'Content-Disposition': 'attachment; filename="playlist.m3u"',
                    'Access-Control-Allow-Origin': '*'
                }
            )
            
        except Exception as e:
            logger.error(f"Error generando playlist: {e}")
            return web.json_response({"error": "Error generando playlist"}, status=500)

    async def handle_channels_list(self, request):
        """Listar todos los canales disponibles"""
        channels_info = []
        for stream_id, channel in self.channels.items():
            host = request.headers.get('Host', 'localhost:8080')
            scheme = 'https' if 'render' in host else 'http'
            
            channels_info.append({
                "name": channel.name,
                "stream_id": stream_id,
                "status": channel.status,
                "url": f"{scheme}://{host}/stream/{stream_id}",
                "last_updated": channel.last_updated
            })
        
        return web.json_response({
            "total_channels": len(channels_info),
            "channels": channels_info
        })

    async def handle_status(self, request):
        """Endpoint de estado del servidor"""
        active_channels = sum(1 for ch in self.channels.values() if ch.status == "active")
        
        return web.json_response({
            "status": "running",
            "total_channels": len(self.channels),
            "active_channels": active_channels,
            "timestamp": datetime.now().isoformat(),
            "uptime": time.time()
        })

    async def cleanup(self):
        """Limpiar recursos"""
        if self.session:
            await self.session.close()
        
        for session in self.proxy_sessions.values():
            await session.close()
        
        logger.info("Recursos limpiados")

def create_app():
    """Crear aplicación web"""
    proxy_server = M3U8ProxyServer()
    app = web.Application()
    
    # Rutas
    app.router.add_get('/', lambda r: web.json_response({
        "message": "Proxy M3U8 la14hd.com",
        "endpoints": {
            "/channels": "Lista de canales",
            "/playlist.m3u": "Descargar playlist M3U",
            "/stream/{stream_id}": "Stream de canal específico",
            "/status": "Estado del servidor"
        }
    }))
    
    app.router.add_get('/channels', proxy_server.handle_channels_list)
    app.router.add_get('/playlist.m3u', proxy_server.handle_playlist_m3u)
    app.router.add_get('/stream/{stream_id}', proxy_server.handle_channel_stream)
    app.router.add_get('/status', proxy_server.handle_status)
    
    # Middleware para inicialización
    async def init_middleware(request, handler):
        if not hasattr(app, '_initialized'):
            await proxy_server.initialize()
            app._initialized = True
            app._proxy_server = proxy_server
        return await handler(request)
    
    app.middlewares.append(init_middleware)
    
    # Handler de cierre
    async def cleanup_handler(app):
        if hasattr(app, '_proxy_server'):
            await app._proxy_server.cleanup()
    
    app.on_cleanup.append(cleanup_handler)
    
    return app

if __name__ == '__main__':
    # Para desarrollo local
    app = create_app()
    port = int(os.environ.get('PORT', 8080))
    
    print(f"""
    🚀 Proxy M3U8 la14hd.com iniciado
    
    📺 Endpoints disponibles:
    http://localhost:{port}/channels - Lista de canales
    http://localhost:{port}/playlist.m3u - Descargar playlist M3U
    http://localhost:{port}/stream/{{canal}} - Stream específico
    http://localhost:{port}/status - Estado del servidor
    
    📁 Asegúrate de tener el archivo 'canales.txt' con la lista de canales
    """)
    
    web.run_app(app, host='0.0.0.0', port=port)