import asyncio
import logging
import re
import json
from urllib.parse import urlparse
from typing import Dict, Any
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    """Eccezione personalizzata per errori di estrazione."""
    pass

class VixSrcExtractor:
    """VixSrc URL extractor per risolvere link VixSrc."""
    
    def __init__(self, request_headers: dict):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.5",
            "accept-encoding": "gzip, deflate",
            "connection": "keep-alive",
        }
        self.session = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self._session_lock = asyncio.Lock()

    async def _get_session(self):
        """Ottiene una sessione HTTP persistente."""
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
            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers=self.base_headers,
                cookie_jar=aiohttp.CookieJar()
            )
        return self.session

    async def _make_robust_request(self, url: str, headers: dict = None, retries=3, initial_delay=2):
        """Effettua richieste HTTP robuste con retry automatico."""
        final_headers = headers or {}
        
        for attempt in range(retries):
            try:
                session = await self._get_session()
                logger.info(f"Tentativo {attempt + 1}/{retries} per URL: {url}")
                
                async with session.get(url, headers=final_headers) as response:
                    response.raise_for_status()
                    content = await response.text()
                    
                    class MockResponse:
                        def __init__(self, text_content, status, headers_dict, url):
                            self._text = text_content
                            self.status = status
                            self.headers = headers_dict
                            self.url = url
                            self.status_code = status
                            self.text = text_content
                        
                        async def text_async(self):
                            return self._text
                        
                        def raise_for_status(self):
                            if self.status >= 400:
                                raise aiohttp.ClientResponseError(
                                    request_info=None,
                                    history=None,
                                    status=self.status
                                )
                    
                    logger.info(f"✅ Richiesta riuscita per {url} al tentativo {attempt + 1}")
                    return MockResponse(content, response.status, response.headers, response.url)
                    
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientPayloadError,
                asyncio.TimeoutError,
                OSError,
                ConnectionResetError
            ) as e:
                logger.warning(f"⚠️ Errore connessione tentativo {attempt + 1} per {url}: {str(e)}")
                
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

    async def _parse_html_simple(self, html_content: str, tag: str, attrs: dict = None):
        """Parser HTML semplificato senza BeautifulSoup."""
        try:
            if tag == "div" and attrs and attrs.get("id") == "app":
                # Cerca div con id="app"
                pattern = r'<div[^>]*id="app"[^>]*data-page="([^"]*)"[^>]*>'
                match = re.search(pattern, html_content, re.IGNORECASE)
                if match:
                    return {"data-page": match.group(1)}
                    
            elif tag == "iframe":
                # Cerca iframe src
                pattern = r'<iframe[^>]*src="([^"]*)"[^>]*>'
                match = re.search(pattern, html_content, re.IGNORECASE)
                if match:
                    return {"src": match.group(1)}
                    
            elif tag == "script":
                # Cerca primo script tag nel body
                pattern = r'<body[^>]*>.*?<script[^>]*>(.*?)</script>'
                match = re.search(pattern, html_content, re.DOTALL | re.IGNORECASE)
                if match:
                    return match.group(1)
                    
        except Exception as e:
            logger.error(f"Errore parsing HTML: {e}")
            
        return None

    async def version(self, site_url: str) -> str:
        """Ottiene la versione del sito VixSrc parent."""
        base_url = f"{site_url}/request-a-title"
        
        response = await self._make_robust_request(
            base_url,
            headers={
                "Referer": f"{site_url}/",
                "Origin": f"{site_url}",
            },
        )
        
        if response.status_code != 200:
            raise ExtractorError("URL obsoleto")
        
        # Parser HTML semplificato
        app_div = await self._parse_html_simple(response.text, "div", {"id": "app"})
        if app_div and app_div.get("data-page"):
            try:
                # Decodifica HTML entities se necessario
                data_page = app_div["data-page"].replace("&quot;", '"')
                data = json.loads(data_page)
                return data["version"]
            except (KeyError, json.JSONDecodeError, AttributeError) as e:
                raise ExtractorError(f"Fallimento parsing versione: {e}")
        else:
            raise ExtractorError("Impossibile trovare dati versione")

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        """Estrae URL VixSrc."""
        try:
            version = None
            response = None
            
            # ✅ NUOVO: Gestione per URL di playlist che non richiedono estrazione.
            # Se l'URL è già un manifest, lo restituisce direttamente.
            if "vixsrc.to/playlist" in url:
                logger.info("URL è già un manifest VixSrc, non richiede estrazione.")
                return {
                    "destination_url": url,
                    "request_headers": self.base_headers,
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                }

            if "iframe" in url:
                # Gestione URL iframe
                site_url = url.split("/iframe")[0]
                version = await self.version(site_url)
                
                # Prima richiesta con headers Inertia
                response = await self._make_robust_request(
                    url, 
                    headers={
                        "x-inertia": "true", 
                        "x-inertia-version": version,
                        **self.base_headers
                    }
                )
                
                # Cerca iframe src
                iframe_data = await self._parse_html_simple(response.text, "iframe")
                if iframe_data and iframe_data.get("src"):
                    iframe_url = iframe_data["src"]
                    
                    # Seconda richiesta all'iframe
                    response = await self._make_robust_request(
                        iframe_url, 
                        headers={
                            "x-inertia": "true", 
                            "x-inertia-version": version,
                            **self.base_headers
                        }
                    )
                else:
                    raise ExtractorError("Nessun iframe trovato nella risposta")
                    
            elif "movie" in url or "tv" in url:
                # Gestione URL diretti movie/tv
                response = await self._make_robust_request(url)
            else:
                raise ExtractorError("Tipo URL VixSrc non supportato")
            
            if response.status_code != 200:
                raise ExtractorError("Fallimento estrazione componenti URL, richiesta non valida")
            
            # Estrai script dal body
            script_content = await self._parse_html_simple(response.text, "script")
            if not script_content:
                raise ExtractorError("Nessuno script trovato nel body")
            
            # Estrai parametri dallo script JavaScript
            try:
                token_match = re.search(r"'token':\s*'(\w+)'", script_content)
                expires_match = re.search(r"'expires':\s*'(\d+)'", script_content)
                server_url_match = re.search(r"url:\s*'([^']+)'", script_content)
                
                if not all([token_match, expires_match, server_url_match]):
                    raise ExtractorError("Parametri mancanti nello script JS")
                
                token = token_match.group(1)
                expires = expires_match.group(1)
                server_url = server_url_match.group(1)
                
                # Costruisci URL finale
                if "?b=1" in server_url:
                    final_url = f'{server_url}&token={token}&expires={expires}'
                else:
                    final_url = f"{server_url}?token={token}&expires={expires}"
                
                # Verifica supporto FHD
                if "window.canPlayFHD = true" in script_content:
                    final_url += "&h=1"
                
                # Prepara headers finali
                stream_headers = self.base_headers.copy()
                stream_headers["referer"] = url
                
                logger.info(f"✅ URL VixSrc estratto con successo: {final_url}")
                
                return {
                    "destination_url": final_url,
                    "request_headers": stream_headers,
                    "mediaflow_endpoint": self.mediaflow_endpoint,
                }
                
            except Exception as e:
                raise ExtractorError(f"Errore parsing script JavaScript: {e}")
                
        except Exception as e:
            logger.error(f"❌ Estrazione VixSrc fallita: {str(e)}")
            raise ExtractorError(f"Estrazione VixSrc completamente fallita: {str(e)}")

    async def close(self):
        """Chiude definitivamente la sessione."""
        if self.session and not self.session.closed:
            try:
                await self.session.close()
            except:
                pass
            self.session = None