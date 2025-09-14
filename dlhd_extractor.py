import asyncio
import logging
import re
import base64
import json
from urllib.parse import urlparse, quote_plus
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from typing import Dict, Any

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class DLHDExtractor:
    """DLHD Extractor con sessione persistente e gestione anti-bot avanzata"""

    def __init__(self, request_headers: dict):
        self.request_headers = request_headers
        self.base_headers = {
            # ✅ User-Agent più recente per bypassare protezioni anti-bot
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        }
        self.session = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self._cached_base_url = None
        self._iframe_context = None
        self._session_lock = asyncio.Lock()

    async def _get_session(self):
        """✅ Sessione persistente con cookie jar automatico"""
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            connector = TCPConnector(
                limit=10,
                limit_per_host=3,
                keepalive_timeout=30,
                enable_cleanup_closed=True,
                force_close=False,
                use_dns_cache=True
            )
            # ✅ FONDAMENTALE: Cookie jar per mantenere sessione come browser reale
            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers=self.base_headers,
                cookie_jar=aiohttp.CookieJar()
            )
        return self.session

    def _get_headers_for_url(self, url: str, base_headers: dict) -> dict:
        """Applica headers specifici per newkso.ru automaticamente"""
        headers = base_headers.copy()
        parsed_url = urlparse(url)
        
        if "newkso.ru" in parsed_url.netloc:
            if self._iframe_context:
                iframe_origin = f"https://{urlparse(self._iframe_context).netloc}"
                newkso_headers = {
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
                    'Referer': self._iframe_context,
                    'Origin': iframe_origin
                }
                logger.info(f"Applied newkso.ru headers with iframe context for: {url}")
            else:
                newkso_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
                newkso_headers = {
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
                    'Referer': newkso_origin,
                    'Origin': newkso_origin
                }
            headers.update(newkso_headers)
        
        return headers

    async def _make_robust_request(self, url: str, headers: dict = None, retries=3, initial_delay=2):
        """✅ Richieste con sessione persistente per evitare anti-bot"""
        final_headers = self._get_headers_for_url(url, headers or {})
        
        for attempt in range(retries):
            try:
                # ✅ IMPORTANTE: Riusa sempre la stessa sessione
                session = await self._get_session()
                
                logger.info(f"Tentativo {attempt + 1}/{retries} per URL: {url}")
                
                async with session.get(url, headers=final_headers) as response:
                    response.raise_for_status()
                    content = await response.text()
                    
                    class MockResponse:
                        def __init__(self, text_content, status, headers_dict):
                            self._text = text_content
                            self.status = status
                            self.headers = headers_dict
                            self.url = url
                        
                        async def text(self):
                            return self._text
                            
                        def raise_for_status(self):
                            if self.status >= 400:
                                raise aiohttp.ClientResponseError(
                                    request_info=None, 
                                    history=None,
                                    status=self.status
                                )
                        
                        async def json(self):
                            return json.loads(self._text)
                    
                    logger.info(f"✅ Richiesta riuscita per {url} al tentativo {attempt + 1}")
                    return MockResponse(content, response.status, response.headers)
                    
            except (
                aiohttp.ClientConnectionError, 
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientPayloadError,
                asyncio.TimeoutError,
                OSError,
                ConnectionResetError
            ) as e:
                logger.warning(f"⚠️ Errore connessione tentativo {attempt + 1} per {url}: {str(e)}")
                
                # ✅ Solo in caso di errore critico, chiudi la sessione
                if attempt == retries - 1:
                    if self.session and not self.session.closed:
                        try:
                            await self.session.close()
                        except:
                            pass
                    self.session = None
                
                if attempt < retries - 1:
                    delay = initial_delay * (2 ** attempt)
                    logger.info(f"⏳ Aspetto {delay} secondi prima del prossimo tentativo...")
                    await asyncio.sleep(delay)
                else:
                    raise ExtractorError(f"Tutti i {retries} tentativi falliti per {url}: {str(e)}")
                    
            except Exception as e:
                logger.error(f"❌ Errore non di rete tentativo {attempt + 1} per {url}: {str(e)}")
                if attempt == retries - 1:
                    raise ExtractorError(f"Errore finale per {url}: {str(e)}")
                await asyncio.sleep(initial_delay)

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        
        async def get_daddylive_base_url():
            if self._cached_base_url:
                return self._cached_base_url
            try:
                resp = await self._make_robust_request("https://daddylive.sx/")
                base_url = "https://daddylive.sx/"
                if hasattr(resp, 'url') and resp.url:
                    parsed = urlparse(str(resp.url))
                    base_url = f"{parsed.scheme}://{parsed.netloc}/"
                self._cached_base_url = base_url
                return base_url
            except Exception as e:
                logger.warning(f"Fallback a URL base predefinito: {str(e)}")
                return "https://daddylive.sx/"

        def extract_channel_id(url_str):
            patterns = [
                r'/premium(\d+)/mono\.m3u8$',
                r'/(?:watch|stream|cast|player)/stream-(\d+)\.php',
                r'(?:%2F|/)stream-(\d+)\.php',
                r'stream-(\d+)\.php'
            ]
            for pattern in patterns:
                match = re.search(pattern, url_str, re.IGNORECASE)
                if match:
                    return match.group(1)
            return None

        async def try_endpoint(baseurl, endpoint, channel_id):
            stream_url = f"{baseurl}{endpoint}stream-{channel_id}.php"
            daddy_origin = urlparse(baseurl).scheme + "://" + urlparse(baseurl).netloc
            
            daddylive_headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
                'Referer': baseurl,
                'Origin': daddy_origin
            }

            # Fase 1: Ottieni pagina principale
            resp1 = await self._make_robust_request(stream_url, headers=daddylive_headers)
            content1 = await resp1.text()
            
            iframes = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>\s*<button[^>]*>\s*Player\s*2\s*</button>', content1)
            if not iframes:
                raise ExtractorError("No Player 2 link found")
            
            url2 = iframes[0]
            if not url2.startswith('http'):
                url2 = baseurl + url2.lstrip('/')
            url2 = url2.replace('//cast', '/cast')
            
            daddylive_headers['Referer'] = url2
            daddylive_headers['Origin'] = urlparse(url2).scheme + "://" + urlparse(url2).netloc
            
            # Fase 2: Ottieni pagina Player 2
            resp2 = await self._make_robust_request(url2, headers=daddylive_headers)
            content2 = await resp2.text()
            
            iframes2 = re.findall(r'iframe src="([^"]*)', content2)
            if not iframes2:
                raise ExtractorError("No iframe found in Player 2 page")
            
            iframe_url = iframes2[0]
            if not iframe_url.startswith('http'):
                iframe_url = urlparse(url2).scheme + "://" + urlparse(url2).netloc + "/" + iframe_url.lstrip('/')
                
            self._iframe_context = iframe_url
            
            # Fase 3: Ottieni contenuto iframe
            resp3 = await self._make_robust_request(iframe_url, headers=daddylive_headers)
            iframe_content = await resp3.text()

            # ✅ Funzioni di estrazione parametri robuste
            def extract_var_old_format(js, name):
                """Estrae variabili dal vecchio formato con atob()"""
                patterns = [
                    rf'var (?:__)?{name}\s*=\s*atob\("([^"]+)"\)',
                    rf'var (?:__)?{name}\s*=\s*atob\(\'([^\']+)\'\)',
                    rf'(?:var|let|const)\s+(?:__)?{name}\s*=\s*atob\("([^"]+)"\)'
                ]
                for pattern in patterns:
                    try:
                        match = re.search(pattern, js)
                        if match:
                            return base64.b64decode(match.group(1)).decode('utf-8')
                    except Exception:
                        continue
                return None
            
            def extract_xjz_format(js):
                """Estrae parametri dal formato XJZ"""
                try:
                    xjz_pattern = r'const\s+XJZ\s*=\s*["\']([^"\']+)["\']'
                    match = re.search(xjz_pattern, js)
                    if not match:
                        return None
                    
                    xjz_b64 = match.group(1)
                    xjz_json = base64.b64decode(xjz_b64).decode('utf-8')
                    xjz_obj = json.loads(xjz_json)
                    
                    decoded = {}
                    for k, v in xjz_obj.items():
                        try:
                            decoded[k] = base64.b64decode(v).decode('utf-8')
                        except Exception:
                            decoded[k] = v
                    return decoded
                except Exception:
                    return None

            def extract_bundle_format(js):
                """Estrae parametri dal formato BUNDLE"""
                try:
                    bundle_patterns = [
                        r'const\s+BUNDLE\s*=\s*["\']([^"\']+)["\']',
                        r'var\s+BUNDLE\s*=\s*["\']([^"\']+)["\']',
                        r'let\s+BUNDLE\s*=\s*["\']([^"\']+)["\']'
                    ]
                    bundle_data = None
                    for pattern in bundle_patterns:
                        match = re.search(pattern, js)
                        if match:
                            bundle_data = match.group(1)
                            break
                    
                    # ✅ CORREZIONE: Usa bundle_data invece di bundle_
                    if not bundle_data:
                        return None
                    
                    bundle_json = base64.b64decode(bundle_data).decode('utf-8')
                    bundle_obj = json.loads(bundle_json)
                    decoded_bundle = {}
                    for key, value in bundle_obj.items():
                        try:
                            decoded_bundle[key] = base64.b64decode(value).decode('utf-8')
                        except Exception:
                            decoded_bundle[key] = value
                    return decoded_bundle
                except Exception:
                    return None
            
            try:
                # Estrai channel key
                channel_key = None
                channel_key_patterns = [
                    r'const\s+CHANNEL_KEY\s*=\s*["\']([^"\']+)["\']',
                    r'var\s+CHANNEL_KEY\s*=\s*["\']([^"\']+)["\']',
                    r'let\s+CHANNEL_KEY\s*=\s*["\']([^"\']+)["\']',
                    r'channelKey\s*=\s*["\']([^"\']+)["\']',
                    r'var\s+channelKey\s*=\s*["\']([^"\']+)["\']',
                    r'(?:let|const)\s+channelKey\s*=\s*["\']([^"\']+)["\']'
                ]
                for pattern in channel_key_patterns:
                    match = re.search(pattern, iframe_content)
                    if match:
                        channel_key = match.group(1)
                        break
                
                # Inizializza tutte le variabili a None
                auth_host = auth_php = auth_ts = auth_rnd = auth_sig = None
                
                # Prova formato XJZ
                xjz_data = extract_xjz_format(iframe_content)
                if xjz_data:
                    logger.info("Uso del nuovo formato XJZ per l'estrazione dei parametri")
                    auth_host = xjz_data.get('b_host')
                    auth_php = xjz_data.get('b_script')
                    auth_ts = xjz_data.get('b_ts')
                    auth_rnd = xjz_data.get('b_rnd')
                    auth_sig = xjz_data.get('b_sig')
                else:
                    # Prova formato BUNDLE
                    bundle_data = extract_bundle_format(iframe_content)
                    # ✅ CORREZIONE: Usa bundle_data invece di bundle_
                    if bundle_data:
                        logger.info("Uso del formato BUNDLE per l'estrazione dei parametri")
                        auth_host = bundle_data.get('b_host')
                        auth_php = bundle_data.get('b_script')
                        auth_ts = bundle_data.get('b_ts')
                        auth_rnd = bundle_data.get('b_rnd')
                        auth_sig = bundle_data.get('b_sig')
                    else:
                        # Fallback al formato vecchio
                        logger.info("Fallback al formato vecchio per l'estrazione dei parametri")
                        auth_ts = extract_var_old_format(iframe_content, 'c')
                        auth_rnd = extract_var_old_format(iframe_content, 'd')
                        auth_sig = extract_var_old_format(iframe_content, 'e')
                        auth_host = extract_var_old_format(iframe_content, 'a')
                        auth_php = extract_var_old_format(iframe_content, 'b')

                # Verifica che tutti i parametri siano presenti
                missing_params = []
                if not channel_key:
                    missing_params.append('channel_key')
                if not auth_ts:
                    missing_params.append('auth_ts')
                if not auth_rnd:
                    missing_params.append('auth_rnd')
                if not auth_sig:
                    missing_params.append('auth_sig')
                if not auth_host:
                    missing_params.append('auth_host')
                if not auth_php:
                    missing_params.append('auth_php')

                if missing_params:
                    raise ExtractorError(f"Parametri mancanti: {', '.join(missing_params)}")

                # Procedi con l'autenticazione
                auth_sig = quote_plus(auth_sig)
                
                if auth_php:
                    normalized_auth_php = auth_php.strip().lstrip('/')
                    if normalized_auth_php == 'a.php':
                        auth_php = '/auth.php'
                
                if auth_host.endswith('/') and auth_php.startswith('/'):
                    auth_url = f'{auth_host[:-1]}{auth_php}'
                elif not auth_host.endswith('/') and not auth_php.startswith('/'):
                    auth_url = f'{auth_host}/{auth_php}'
                else:
                    auth_url = f'{auth_host}{auth_php}'
                
                auth_url = f'{auth_url}?channel_id={channel_key}&ts={auth_ts}&rnd={auth_rnd}&sig={auth_sig}'
                
                # Fase 4: Auth request
                auth_resp = await self._make_robust_request(auth_url, headers=daddylive_headers)
                
                # Fase 5: Server lookup
                server_lookup_url = f"https://{urlparse(iframe_url).netloc}/server_lookup.php?channel_id={channel_key}"
                
                lookup_resp = await self._make_robust_request(server_lookup_url, headers=daddylive_headers)
                server_data = await lookup_resp.json()
                server_key = server_data.get('server_key')
                
                if not server_key:
                    raise ExtractorError("Nessun server_key trovato")
                
                logger.info(f"Server key ottenuto: {server_key}")
                
                referer_raw = f'https://{urlparse(iframe_url).netloc}'
                
                # Costruisci URL finale del stream
                if server_key == 'top1/cdn':
                    clean_m3u8_url = f'https://top1.newkso.ru/top1/cdn/{channel_key}/mono.m3u8'
                else:
                    clean_m3u8_url = f'https://{server_key}new.newkso.ru/{server_key}/{channel_key}/mono.m3u8'
                
                # ✅ Headers finali ottimizzati per newkso.ru
                if "newkso.ru" in clean_m3u8_url:
                    stream_headers = {
                        'User-Agent': daddylive_headers['User-Agent'],
                        'Referer': iframe_url,
                        'Origin': referer_raw
                    }
                else:
                    stream_headers = {
                        'User-Agent': daddylive_headers['User-Agent'],
                        'Referer': referer_raw,
                        'Origin': referer_raw
                    }
                
                logger.info(f"🔧 Headers finali per stream: {stream_headers}")
                logger.info(f"✅ Stream URL finale: {clean_m3u8_url}")
                
                return {
                    "destination_url": clean_m3u8_url,
                    "request_headers": stream_headers,
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                }
                
            except Exception as param_error:
                logger.error(f"Errore nell'estrazione parametri: {str(param_error)}")
                raise ExtractorError(f"Fallimento estrazione parametri: {str(param_error)}")

        try:
            clean_url = url
            channel_id = extract_channel_id(clean_url)
            if not channel_id:
                raise ExtractorError(f"Impossibile estrarre channel ID da {clean_url}")

            baseurl = await get_daddylive_base_url()
            
            # Prova tutti gli endpoint in sequenza
            endpoints = ["stream/", "cast/", "player/", "watch/"]
            last_exc = None
            
            for endpoint in endpoints:
                try:
                    logger.info(f"🚀 Provo endpoint: {endpoint}")
                    result = await try_endpoint(baseurl, endpoint, channel_id)
                    logger.info(f"✅ Endpoint {endpoint} riuscito!")
                    return result
                except Exception as exc:
                    logger.warning(f"❌ Endpoint {endpoint} fallito: {str(exc)}")
                    last_exc = exc
                    continue
                    
            # Se tutti gli endpoint falliscono
            if last_exc:
                raise ExtractorError(f"Tutti gli endpoint DLHD hanno fallito. Ultimo errore: {str(last_exc)}")
            else:
                raise ExtractorError("Tutti gli endpoint DLHD hanno fallito senza dettagli.")
            
        except Exception as e:
            raise ExtractorError(f"Estrazione DLHD completamente fallita: {str(e)}")

    async def close(self):
        """Chiude definitivamente la sessione"""
        if self.session and not self.session.closed:
            try:
                await self.session.close()
            except:
                pass
        self.session = None
