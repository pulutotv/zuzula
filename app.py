import asyncio
import logging
import re
import sys
import os
import urllib.parse
from urllib.parse import urlparse, urljoin
import xml.etree.ElementTree as ET
import aiohttp
from aiohttp import web
from aiohttp import web, ClientSession, ClientTimeout, TCPConnector

# Configurazione logging
logging.basicConfig(level=logging.INFO)

# --- AUTO-UPDATER DA GITHUB ---
# Esegue il controllo degli aggiornamenti prima di caricare qualsiasi altro modulo locale.
try:
    # Aggiunge il percorso corrente a sys.path per permettere l'importazione di 'updater'
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from updater import check_for_updates
    check_for_updates()
except ImportError:
    logging.warning("⚠️ Modulo 'updater.py' non trovato o dipendenza 'requests' mancante. L'aggiornamento automatico è disabilitato.")
except Exception as e:
    logging.error(f"❌ Si è verificato un errore imprevisto durante l'auto-update: {e}")
# --- FINE AUTO-UPDATER ---

logger = logging.getLogger(__name__)

# Aggiungi path corrente per import moduli
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --- Moduli Esterni ---
# Vengono importati singolarmente per un feedback più granulare in caso di errore.
VavooExtractor, DLHDExtractor, VixSrcExtractor, PlaylistBuilder = None, None, None, None

try:
    from vavoo_extractor import VavooExtractor
    logger.info("✅ Modulo VavooExtractor caricato.")
except ImportError:
    logger.warning("⚠️ Modulo VavooExtractor non trovato. Funzionalità Vavoo disabilitata.")

try:
    from dlhd_extractor import DLHDExtractor
    logger.info("✅ Modulo DLHDExtractor caricato.")
except ImportError:
    logger.warning("⚠️ Modulo DLHDExtractor non trovato. Funzionalità DLHD disabilitata.")

try:
    from playlist_builder import PlaylistBuilder
    logger.info("✅ Modulo PlaylistBuilder caricato.")
except ImportError:
    logger.warning("⚠️ Modulo PlaylistBuilder non trovato. Funzionalità PlaylistBuilder disabilitata.")
    
try:
    from vixsrc_extractor import VixSrcExtractor
    logger.info("✅ Modulo VixSrcExtractor caricato.")
except ImportError:
    logger.warning("⚠️ Modulo VixSrcExtractor non trovato. Funzionalità VixSrc disabilitata.")

# --- Classi Unite ---
class ExtractorError(Exception):
    """Eccezione personalizzata per errori di estrazione"""
    pass

class GenericHLSExtractor:
    def __init__(self, request_headers):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        self.session = None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            connector = TCPConnector(
                limit=20, limit_per_host=10, 
                keepalive_timeout=60, enable_cleanup_closed=True, 
                force_close=False, use_dns_cache=True
            )
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            self.session = ClientSession(
                timeout=timeout, connector=connector, 
                headers={'user-agent': self.base_headers['user-agent']}
            )
        return self.session

    async def extract(self, url):
        # ✅ CORREZIONE: Permette anche gli URL di playlist VixSrc che non hanno estensione.
        if not any(pattern in url.lower() for pattern in ['.m3u8', '.mpd', '.ts', 'vixsrc.to/playlist']):
            raise ExtractorError("URL non supportato (richiesto .m3u8, .mpd, .ts o URL VixSrc valido)")

        parsed_url = urlparse(url)
        origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        headers = self.base_headers.copy()
        headers.update({"referer": origin, "origin": origin})

        for h, v in self.request_headers.items():
            if h.lower() in ["authorization", "x-api-key", "x-auth-token"]:
                headers[h] = v

        return {
            "destination_url": url, 
            "request_headers": headers, 
            "mediaflow_endpoint": "hls_proxy"
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

class HLSProxy:
    """Proxy HLS per gestire stream Vavoo, DLHD, HLS generici e playlist builder con supporto AES-128"""
    
    def __init__(self):
        self.extractors = {}
        
        # Inizializza il playlist_builder se il modulo è disponibile
        if PlaylistBuilder:
            self.playlist_builder = PlaylistBuilder()
            logger.info("✅ PlaylistBuilder inizializzato")
        else:
            self.playlist_builder = None
    
    async def get_extractor(self, url: str, request_headers: dict):
        """Ottiene l'estrattore appropriato per l'URL"""
        try:
            if "vavoo.to" in url:
                key = "vavoo"
                if key not in self.extractors:
                    self.extractors[key] = VavooExtractor(request_headers)
                return self.extractors[key]
            elif any(domain in url for domain in ["daddylive", "dlhd"]) or re.search(r'stream-\d+\.php', url):
                key = "dlhd"
                if key not in self.extractors:
                    self.extractors[key] = DLHDExtractor(request_headers)
                return self.extractors[key]
            # ✅ MODIFICATO: Aggiunto 'vixsrc.to/playlist' per gestire i sub-manifest come HLS generici.
            elif any(ext in url.lower() for ext in ['.m3u8', '.mpd', '.ts']) or 'vixsrc.to/playlist' in url.lower():
                key = "hls_generic"
                if key not in self.extractors:
                    self.extractors[key] = GenericHLSExtractor(request_headers)
                return self.extractors[key]
            elif 'vixsrc.to/' in url.lower() and any(x in url for x in ['/movie/', '/tv/', '/iframe/']):
                key = "vixsrc"
                if key not in self.extractors:
                    self.extractors[key] = VixSrcExtractor(request_headers)
                return self.extractors[key]
            else:
                raise ExtractorError("Tipo di URL non supportato")
        except (NameError, TypeError) as e:
            raise ExtractorError(f"Estrattore non disponibile - modulo mancante: {e}")

    async def handle_proxy_request(self, request):
        """Gestisce le richieste proxy principali"""
        extractor = None
        try:
            target_url = request.query.get('url')
            if not target_url:
                return web.Response(text="Parametro 'url' mancante", status=400)
            
            try:
                target_url = urllib.parse.unquote(target_url)
            except:
                pass
                
            logger.info(f"Richiesta proxy per URL: {target_url}")
            
            extractor = await self.get_extractor(target_url, dict(request.headers))
            result = await extractor.extract(target_url)
            stream_url = result["destination_url"]
            stream_headers = result.get("request_headers", {})
            
            # Aggiungi headers personalizzati da query params
            for param_name, param_value in request.query.items():
                if param_name.startswith('h_'):
                    header_name = param_name[2:]
                    stream_headers[header_name] = param_value
            
            logger.info(f"Stream URL risolto: {stream_url}")
            return await self._proxy_stream(request, stream_url, stream_headers)
            
        except Exception as e:
            # ✅ AGGIORNATO: Se un estrattore specifico (DLHD, Vavoo) fallisce, riavvia il server per forzare un aggiornamento.
            # Questo è utile se il sito ha cambiato qualcosa e l'estrattore è obsoleto.
            restarting = False
            extractor_name = "sconosciuto"
            if DLHDExtractor and isinstance(extractor, DLHDExtractor):
                restarting = True
                extractor_name = "DLHDExtractor"
            elif VavooExtractor and isinstance(extractor, VavooExtractor):
                restarting = True
                extractor_name = "VavooExtractor"

            if restarting:
                logger.critical(f"❌ Errore critico con {extractor_name}: {e}. Riavvio per forzare l'aggiornamento...")
                await asyncio.sleep(1)  # Attesa per il flush dei log
                os._exit(1)  # Uscita forzata per innescare il riavvio dal process manager (Docker, Gunicorn)

            logger.exception(f"Errore nella richiesta proxy: {str(e)}")
            return web.Response(text=f"Errore proxy: {str(e)}", status=500)

    async def handle_key_request(self, request):
        """✅ NUOVO: Gestisce richieste per chiavi AES-128"""
        key_url = request.query.get('key_url')
        
        if not key_url:
            return web.Response(text="Missing key_url parameter", status=400)
        
        try:
            # Decodifica l'URL se necessario
            try:
                key_url = urllib.parse.unquote(key_url)
            except:
                pass
                
            # Inizializza gli header esclusivamente da quelli passati dinamicamente
            # tramite l'URL. Se l'estrattore non li passa, la richiesta
            # verrà fatta senza header specifici, affidandosi alla correttezza
            # del flusso di estrazione.
            headers = {}
            for param_name, param_value in request.query.items():
                if param_name.startswith('h_'):
                    header_name = param_name[2:].replace('_', '-')
                    headers[header_name] = param_value

            logger.info(f"🔑 Fetching AES key from: {key_url}")
            logger.debug(f"   -> with headers: {headers}")
            
            timeout = ClientTimeout(total=30)
            async with ClientSession(timeout=timeout) as session:
                async with session.get(key_url, headers=headers) as resp:
                    if resp.status == 200:
                        key_data = await resp.read()
                        logger.info(f"✅ AES key fetched successfully: {len(key_data)} bytes")
                        
                        return web.Response(
                            body=key_data,
                            content_type="application/octet-stream",
                            headers={
                                "Access-Control-Allow-Origin": "*",
                                "Access-Control-Allow-Headers": "*",
                                "Cache-Control": "no-cache, no-store, must-revalidate"
                            }
                        )
                    else:
                        logger.error(f"❌ Key fetch failed with status: {resp.status}")
                        return web.Response(text=f"Key fetch failed: {resp.status}", status=resp.status)
                        
        except Exception as e:
            logger.error(f"❌ Error fetching AES key: {str(e)}")
            return web.Response(text=f"Key error: {str(e)}", status=500)

    async def handle_ts_segment(self, request):
        """Gestisce richieste per segmenti .ts"""
        try:
            segment_name = request.match_info.get('segment')
            base_url = request.query.get('base_url')
            
            if not base_url:
                return web.Response(text="Base URL mancante per segmento", status=400)
            
            base_url = urllib.parse.unquote(base_url)
            
            if base_url.endswith('/'):
                segment_url = f"{base_url}{segment_name}"
            else:
                segment_url = f"{base_url.rsplit('/', 1)[0]}/{segment_name}"
            
            # Gestisce la risposta del proxy per il segmento
            return await self._proxy_segment(request, segment_url, {
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "referer": base_url
            }, segment_name)
            
        except Exception as e:
            logger.error(f"Errore nel proxy segmento .ts: {str(e)}")
            return web.Response(text=f"Errore segmento: {str(e)}", status=500)

    async def _proxy_segment(self, request, segment_url, stream_headers, segment_name):
        """✅ NUOVO: Proxy dedicato per segmenti .ts con Content-Disposition"""
        try:
            headers = dict(stream_headers)
            
            # Passa attraverso alcuni headers del client
            for header in ['range', 'if-none-match', 'if-modified-since']:
                if header in request.headers:
                    headers[header] = request.headers[header]
            
            timeout = ClientTimeout(total=60, connect=30)
            async with ClientSession(timeout=timeout) as session:
                async with session.get(segment_url, headers=headers) as resp:
                    response_headers = {}
                    
                    for header in ['content-type', 'content-length', 'content-range', 
                                 'accept-ranges', 'last-modified', 'etag']:
                        if header in resp.headers:
                            response_headers[header] = resp.headers[header]
                    
                    # Forza il content-type e aggiunge Content-Disposition per .ts
                    response_headers['Content-Type'] = 'video/MP2T'
                    response_headers['Content-Disposition'] = f'attachment; filename="{segment_name}"'
                    response_headers['Access-Control-Allow-Origin'] = '*'
                    response_headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
                    response_headers['Access-Control-Allow-Headers'] = 'Range, Content-Type'
                    
                    response = web.StreamResponse(
                        status=resp.status,
                        headers=response_headers
                    )
                    
                    await response.prepare(request)
                    
                    async for chunk in resp.content.iter_chunked(8192):
                        await response.write(chunk)
                    
                    await response.write_eof()
                    return response
                    
        except Exception as e:
            logger.error(f"Errore nel proxy del segmento: {str(e)}")
            return web.Response(text=f"Errore segmento: {str(e)}", status=500)

    async def _proxy_stream(self, request, stream_url, stream_headers):
        """Effettua il proxy dello stream con gestione manifest e AES-128"""
        try:
            headers = dict(stream_headers)
            
            # Passa attraverso alcuni headers del client
            for header in ['range', 'if-none-match', 'if-modified-since']:
                if header in request.headers:
                    headers[header] = request.headers[header]
            
            timeout = ClientTimeout(total=60, connect=30)
            async with ClientSession(timeout=timeout) as session:
                async with session.get(stream_url, headers=headers) as resp:
                    content_type = resp.headers.get('content-type', '')
                    
                    # Gestione special per manifest HLS
                    if 'mpegurl' in content_type or stream_url.endswith('.m3u8'):
                        manifest_content = await resp.text()
                        
                        scheme = request.scheme
                        host = request.host
                        proxy_base = f"{scheme}://{host}"
                        
                        rewritten_manifest = self._rewrite_manifest_urls(
                            manifest_content, stream_url, proxy_base, headers
                        )
                        
                        return web.Response(
                            text=rewritten_manifest,
                            headers={
                                'Content-Type': 'application/vnd.apple.mpegurl',
                                'Content-Disposition': 'attachment; filename="stream.m3u8"',
                                'Access-Control-Allow-Origin': '*',
                                'Cache-Control': 'no-cache'
                            }
                        )
                    
                    # ✅ AGGIORNATO: Gestione per manifest MPD (DASH)
                    elif 'dash+xml' in content_type or stream_url.endswith('.mpd'):
                        manifest_content = await resp.text()
                        
                        scheme = request.scheme
                        host = request.host
                        proxy_base = f"{scheme}://{host}"
                        
                        rewritten_manifest = self._rewrite_mpd_manifest(manifest_content, stream_url, proxy_base, headers)
                        
                        return web.Response(
                            text=rewritten_manifest,
                            headers={
                                'Content-Type': 'application/dash+xml',
                                'Content-Disposition': 'attachment; filename="stream.mpd"',
                                'Access-Control-Allow-Origin': '*',
                                'Cache-Control': 'no-cache'
                            })
                    
                    # Streaming normale per altri tipi di contenuto
                    response_headers = {}
                    
                    for header in ['content-type', 'content-length', 'content-range', 
                                 'accept-ranges', 'last-modified', 'etag']:
                        if header in resp.headers:
                            response_headers[header] = resp.headers[header]
                    
                    response_headers['Access-Control-Allow-Origin'] = '*'
                    response_headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
                    response_headers['Access-Control-Allow-Headers'] = 'Range, Content-Type'
                    
                    response = web.StreamResponse(
                        status=resp.status,
                        headers=response_headers
                    )
                    
                    await response.prepare(request)
                    
                    async for chunk in resp.content.iter_chunked(8192):
                        await response.write(chunk)
                    
                    await response.write_eof()
                    return response
                    
        except Exception as e:
            logger.error(f"Errore nel proxy dello stream: {str(e)}")
            return web.Response(text=f"Errore stream: {str(e)}", status=500)

    def _rewrite_mpd_manifest(self, manifest_content: str, base_url: str, proxy_base: str, stream_headers: dict) -> str:
        """Riscrive i manifest MPD (DASH) per passare attraverso il proxy."""
        try:
            # Aggiungiamo il namespace di default se non presente, per ET
            if 'xmlns' not in manifest_content:
                manifest_content = manifest_content.replace('<MPD', '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1)

            root = ET.fromstring(manifest_content)
            ns = {'mpd': 'urn:mpeg:dash:schema:mpd:2011'}

            # Includiamo solo gli header rilevanti per evitare URL troppo lunghi
            header_params = "".join([f"&h_{urllib.parse.quote(key)}={urllib.parse.quote(value)}" for key, value in stream_headers.items() if key.lower() in ['user-agent', 'referer', 'origin', 'authorization']])

            def create_proxy_url(relative_url):
                absolute_url = urljoin(base_url, relative_url)
                encoded_url = urllib.parse.quote(absolute_url, safe='')
                return f"{proxy_base}/proxy/manifest.m3u8?url={encoded_url}{header_params}"

            # Riscrive gli attributi 'media' e 'initialization' in <SegmentTemplate>
            for template_tag in root.findall('.//mpd:SegmentTemplate', ns):
                for attr in ['media', 'initialization']:
                    if template_tag.get(attr):
                        template_tag.set(attr, create_proxy_url(template_tag.get(attr)))
            
            # Riscrive l'attributo 'media' in <SegmentURL>
            for seg_url_tag in root.findall('.//mpd:SegmentURL', ns):
                if seg_url_tag.get('media'):
                    seg_url_tag.set('media', create_proxy_url(seg_url_tag.get('media')))

            return ET.tostring(root, encoding='unicode', method='xml')

        except Exception as e:
            logger.error(f"❌ Errore durante la riscrittura del manifest MPD: {e}")
            return manifest_content # Restituisce il contenuto originale in caso di errore

    def _rewrite_manifest_urls(self, manifest_content: str, base_url: str, proxy_base: str, stream_headers: dict) -> str:
        """✅ AGGIORNATA: Riscrive gli URL nei manifest HLS per passare attraverso il proxy (incluse chiavi AES)"""
        lines = manifest_content.split('\n')
        rewritten_lines = []
        
        # Prepara gli header da passare come parametri URL
        header_params = "".join([f"&h_{urllib.parse.quote(key)}={urllib.parse.quote(value)}" for key, value in stream_headers.items() if key.lower() in ['user-agent', 'referer', 'origin', 'authorization']])

        for line in lines:
            line = line.strip()
            
            # ✅ NUOVO: Gestione chiavi AES-128
            if line.startswith('#EXT-X-KEY:') and 'URI=' in line:
                # Trova e sostituisci l'URI della chiave AES
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)
                
                if uri_start > 4 and uri_end > uri_start:
                    original_key_url = line[uri_start:uri_end]
                    
                    # ✅ CORREZIONE: Usa urljoin per costruire l'URL assoluto della chiave in modo sicuro.
                    absolute_key_url = urljoin(base_url, original_key_url)
                    
                    # Crea URL proxy per la chiave
                    encoded_key_url = urllib.parse.quote(absolute_key_url, safe='')
                    proxy_key_url = f"{proxy_base}/key?key_url={encoded_key_url}"

                    # Aggiungi gli header necessari come parametri h_
                    # Questo permette al gestore della chiave di usare il contesto corretto
                    key_req_headers = {}
                    for h_name in ['Referer', 'Origin', 'User-Agent']:
                        if h_name in stream_headers:
                            # Usa h_User_Agent per evitare conflitti con trattini
                            param_name = f"h_{h_name.replace('-', '_')}"
                            key_req_headers[param_name] = stream_headers[h_name]
                    
                    if key_req_headers:
                        proxy_key_url += "&" + urllib.parse.urlencode(key_req_headers)
                    
                    # Sostituisci l'URI nel tag EXT-X-KEY
                    new_line = line[:uri_start] + proxy_key_url + line[uri_end:]
                    rewritten_lines.append(new_line)
                    logger.info(f"🔄 Redirected AES key: {absolute_key_url} -> {proxy_key_url}")
                else:
                    rewritten_lines.append(line)
            
            # ✅ NUOVO: Gestione per i sottotitoli e altri media nel tag #EXT-X-MEDIA
            elif line.startswith('#EXT-X-MEDIA:') and 'URI=' in line:
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)
                
                if uri_start > 4 and uri_end > uri_start:
                    original_media_url = line[uri_start:uri_end]
                    
                    # Costruisci l'URL assoluto e poi il proxy URL
                    absolute_media_url = urljoin(base_url, original_media_url)
                    encoded_media_url = urllib.parse.quote(absolute_media_url, safe='')
                    
                    # I sottotitoli sono manifest, quindi usano l'endpoint del proxy principale
                    proxy_media_url = f"{proxy_base}/proxy/manifest.m3u8?url={encoded_media_url}{header_params}"
                    
                    # Sostituisci l'URI nel tag
                    new_line = line[:uri_start] + proxy_media_url + line[uri_end:]
                    rewritten_lines.append(new_line)
                    logger.info(f"🔄 Redirected Media URL: {absolute_media_url} -> {proxy_media_url}")
                else:
                    rewritten_lines.append(line)

            # Gestione segmenti video (.ts) e sub-manifest (.m3u8), sia relativi che assoluti
            elif line and not line.startswith('#') and ('http' in line or not any(c in line for c in ' =?')):
                # ✅ CORREZIONE FINALE: Distingue tra manifest e segmenti.
                # I manifest (.m3u8) e le playlist vixsrc vanno all'endpoint proxy principale.
                if '.m3u8' in line or 'vixsrc.to/playlist' in line:
                    absolute_url = urljoin(base_url, line) if not line.startswith('http') else line
                    encoded_url = urllib.parse.quote(absolute_url, safe='')
                    proxy_url = f"{proxy_base}/proxy/manifest.m3u8?url={encoded_url}{header_params}"
                    rewritten_lines.append(proxy_url)
                # I segmenti .ts vengono gestiti come stream diretti tramite lo stesso endpoint,
                # ma la logica in `handle_proxy_request` e `get_extractor` li instraderà correttamente.
                elif '.ts' in line:
                    absolute_url = urljoin(base_url, line) if not line.startswith('http') else line
                    encoded_url = urllib.parse.quote(absolute_url, safe='')
                    # Usiamo lo stesso endpoint, la logica in `get_extractor` e `_proxy_stream` distinguerà il content-type.
                    proxy_url = f"{proxy_base}/proxy/manifest.m3u8?url={encoded_url}{header_params}"
                    rewritten_lines.append(proxy_url)
                else:
                    rewritten_lines.append(line) # Lascia invariati altri URL assoluti
            else:
                rewritten_lines.append(line)
        
        return '\n'.join(rewritten_lines)

    async def handle_playlist_request(self, request):
        """Gestisce le richieste per il playlist builder"""
        if not self.playlist_builder:
            return web.Response(text="❌ Playlist Builder non disponibile - modulo mancante", status=503)
            
        try:
            url_param = request.query.get('url')
            
            if not url_param:
                return web.Response(text="Parametro 'url' mancante", status=400)
            
            if not url_param.strip():
                return web.Response(text="Parametro 'url' non può essere vuoto", status=400)
            
            playlist_definitions = [def_.strip() for def_ in url_param.split(';') if def_.strip()]
            if not playlist_definitions:
                return web.Response(text="Nessuna definizione playlist valida trovata", status=400)
            
            scheme = request.scheme
            host = request.host
            base_url = f"{scheme}://{host}"
            
            async def generate_response():
                async for line in self.playlist_builder.async_generate_combined_playlist(
                    playlist_definitions, base_url
                ):
                    yield line.encode('utf-8')
            
            response = web.StreamResponse(
                status=200,
                headers={
                    'Content-Type': 'application/vnd.apple.mpegurl',
                    'Content-Disposition': 'attachment; filename="playlist.m3u"',
                    'Access-Control-Allow-Origin': '*'
                }
            )
            
            await response.prepare(request)
            
            async for chunk in generate_response():
                await response.write(chunk)
            
            await response.write_eof()
            return response
            
        except Exception as e:
            logger.error(f"Errore generale nel playlist handler: {str(e)}")
            return web.Response(text=f"Errore: {str(e)}", status=500)

    def _read_template(self, filename: str) -> str:
        """Funzione helper per leggere un file di template."""
        template_path = os.path.join(os.path.dirname(__file__), 'templates', filename)
        with open(template_path, 'r', encoding='utf-8') as f:
            return f.read()

    async def handle_root(self, request):
        """Serve la pagina principale index.html."""
        try:
            html_content = self._read_template('index.html')
            return web.Response(text=html_content, content_type='text/html')
        except Exception as e:
            logger.error(f"❌ Errore critico: impossibile caricare 'index.html': {e}")
            return web.Response(text="<h1>Errore 500</h1><p>Pagina non trovata.</p>", status=500, content_type='text/html')

    async def handle_builder(self, request):
        """Gestisce l'interfaccia web del playlist builder."""
        try:
            html_content = self._read_template('builder.html')
            return web.Response(text=html_content, content_type='text/html')
        except Exception as e:
            logger.error(f"❌ Errore critico: impossibile caricare 'builder.html': {e}")
            return web.Response(text="<h1>Errore 500</h1><p>Impossibile caricare l'interfaccia builder.</p>", status=500, content_type='text/html')

    async def handle_info_page(self, request):
        """Serve la pagina HTML delle informazioni."""
        try:
            html_content = self._read_template('info.html')
            return web.Response(text=html_content, content_type='text/html')
        except Exception as e:
            logger.error(f"❌ Errore critico: impossibile caricare 'info.html': {e}")
            return web.Response(text="<h1>Errore 500</h1><p>Impossibile caricare la pagina info.</p>", status=500, content_type='text/html')

    async def handle_options(self, request):
        """Gestisce richieste OPTIONS per CORS"""
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
            'Access-Control-Allow-Headers': 'Range, Content-Type',
            'Access-Control-Max-Age': '86400'
        }
        return web.Response(headers=headers)

    async def handle_api_info(self, request):
        """Endpoint API che restituisce le informazioni sul server in formato JSON."""
        info = {
            "proxy": "HLS Proxy Server",
            "version": "2.5.0",  # Aggiornata per supporto AES-128
            "status": "✅ Funzionante",
            "features": [
                "✅ Proxy HLS streams",
                "✅ AES-128 key proxying",  # ✅ NUOVO
                "✅ Playlist building",
                "✅ Multi-extractor support",
                "✅ CORS enabled"
            ],
            "extractors_loaded": list(self.extractors.keys()),
            "modules": {
                "playlist_builder": PlaylistBuilder is not None,
                "vavoo_extractor": VavooExtractor is not None,
                "dlhd_extractor": DLHDExtractor is not None,
                "vixsrc_extractor": VixSrcExtractor is not None,
            },
            "endpoints": {
                "/proxy/manifest.m3u8": "Proxy principale - ?url=<URL>",
                "/key": "Proxy chiavi AES-128 - ?key_url=<URL>",  # ✅ NUOVO
                "/playlist": "Playlist builder - ?url=<definizioni>",
                "/builder": "Interfaccia web per playlist builder",
                "/segment/{segment}": "Proxy per segmenti .ts - ?base_url=<URL>",
                "/info": "Pagina HTML con informazioni sul server",
                "/api/info": "Endpoint JSON con informazioni sul server"
            },
            "usage_examples": {
                "proxy": "/proxy/manifest.m3u8?url=https://example.com/stream.m3u8",
                "aes_key": "/key?key_url=https://server.com/key.bin",  # ✅ NUOVO
                "playlist": "/playlist?url=http://example.com/playlist1.m3u8;http://example.com/playlist2.m3u8",
                "custom_headers": "/proxy/manifest.m3u8?url=<URL>&h_Authorization=Bearer%20token"
            }
        }
        return web.json_response(info)

    async def cleanup(self):
        """Pulizia delle risorse"""
        try:
            for extractor in self.extractors.values():
                if hasattr(extractor, 'close'):
                    await extractor.close()
        except Exception as e:
            logger.error(f"Errore durante cleanup: {e}")

# --- Logica di Avvio ---
def create_app():
    """Crea e configura l'applicazione aiohttp."""
    proxy = HLSProxy()
    
    app = web.Application()
    
    # Registra le route
    app.router.add_get('/', proxy.handle_root)
    app.router.add_get('/builder', proxy.handle_builder)
    app.router.add_get('/info', proxy.handle_info_page)
    app.router.add_get('/api/info', proxy.handle_api_info)
    app.router.add_get('/key', proxy.handle_key_request)
    app.router.add_get('/proxy/manifest.m3u8', proxy.handle_proxy_request)
    app.router.add_get('/playlist', proxy.handle_playlist_request)
    app.router.add_get('/segment/{segment}', proxy.handle_ts_segment)
    
    # Gestore OPTIONS generico per CORS
    app.router.add_route('OPTIONS', '/{tail:.*}', proxy.handle_options)
    
    async def cleanup_handler(app):
        await proxy.cleanup()
    app.on_cleanup.append(cleanup_handler)
    
    return app

# Crea l'istanza "privata" dell'applicazione aiohttp.
app = create_app()

def main():
    """Funzione principale per avviare il server."""
    print("🚀 Avvio HLS Proxy Server...")
    print("📡 Server disponibile su: http://localhost:7860")
    print("📡 Oppure: http://server-ip:7860")
    print("🔗 Endpoints:")
    print("   • / - Pagina principale")
    print("   • /builder - Interfaccia web per il builder di playlist")
    print("   • /info - Pagina con informazioni sul server")
    print("   • /proxy/manifest.m3u8?url=<URL> - Proxy principale per stream")
    print("   • /playlist?url=<definizioni> - Generatore di playlist")
    print("=" * 50)
    
    web.run_app(
        app, # Usa l'istanza aiohttp originale per il runner integrato
        host='0.0.0.0',
        port=7860,
        access_log=logger
    )

if __name__ == '__main__':
    main()
