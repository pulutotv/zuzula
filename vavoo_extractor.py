import asyncio
import logging
import time
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class VavooExtractor:
    """Vavoo URL extractor per risolvere link vavoo.to"""
    
    def __init__(self, request_headers: dict):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        self.session = None
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        
    async def _get_session(self):
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            connector = TCPConnector(
                limit=20,
                limit_per_host=10,
                keepalive_timeout=60,
                enable_cleanup_closed=True,
                force_close=False,
                use_dns_cache=True
            )
            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers={'User-Agent': self.base_headers["user-agent"]}
            )
        return self.session

    async def get_auth_signature(self, retries=3, delay=2) -> Optional[str]:
        """Ottiene la signature di autenticazione per l'API Vavoo con retry logic"""
        headers = {
            "user-agent": "okhttp/4.11.0",
            "accept": "application/json", 
            "content-type": "application/json; charset=utf-8",
            "accept-encoding": "gzip"
        }
        current_time = int(time.time() * 1000)
        
        data = {
            "token": "tosFwQCJMS8qrW_AjLoHPQ41646J5dRNha6ZWHnijoYQQQoADQoXYSo7ki7O5-CsgN4CH0uRk6EEoJ0728ar9scCRQW3ZkbfrPfeCXW2VgopSW2FWDqPOoVYIuVPAOnXCZ5g",
            "reason": "app-blur",
            "locale": "de",
            "theme": "dark",
            "metadata": {
                "device": {
                    "type": "Handset",
                    "brand": "google",
                    "model": "Pixel",
                    "name": "sdk_gphone64_arm64",
                    "uniqueId": "d10e5d99ab665233"
                },
                "os": {
                    "name": "android",
                    "version": "13",
                    "abis": ["arm64-v8a", "armeabi-v7a", "armeabi"],
                    "host": "android"
                },
                "app": {
                    "platform": "android",
                    "version": "3.1.21",
                    "buildId": "289515000",
                    "engine": "hbc85",
                    "signatures": ["6e8a975e3cbf07d5de823a760d4c2547f86c1403105020adee5de67ac510999e"],
                    "installer": "app.revanced.manager.flutter"
                },
                "version": {
                    "package": "tv.vavoo.app",
                    "binary": "3.1.21",
                    "js": "3.1.21"
                }
            },
            "appFocusTime": 0,
            "playerActive": False,
            "playDuration": 0,
            "devMode": False,
            "hasAddon": True,
            "castConnected": False,
            "package": "tv.vavoo.app",
            "version": "3.1.21",
            "process": "app",
            "firstAppStart": current_time,
            "lastAppStart": current_time,
            "ipLocation": "",
            "adblockEnabled": True,
            "proxy": {
                "supported": ["ss", "openvpn"],
                "engine": "ss", 
                "ssVersion": 1,
                "enabled": True,
                "autoServer": True,
                "id": "de-fra"
            },
            "iap": {
                "supported": False
            }
        }
        
        for attempt in range(retries):
            try:
                session = await self._get_session()
                
                async with session.post(
                    "https://www.vavoo.tv/api/app/ping",
                    json=data,
                    headers=headers
                ) as resp:
                    resp.raise_for_status()
                    result = await resp.json()
                    addon_sig = result.get("addonSig")
                    
                    if addon_sig:
                        logger.info(f"Signature Vavoo ottenuta con successo (tentativo {attempt + 1})")
                        return addon_sig
                    else:
                        logger.warning(f"Nessuna addonSig nella risposta API Vavoo (tentativo {attempt + 1})")
                        
            except Exception as e:
                logger.warning(f"Tentativo {attempt + 1} fallito per signature Vavoo: {str(e)}")
                if attempt < retries - 1:
                    await asyncio.sleep(delay * (attempt + 1))
                    if self.session and not self.session.closed:
                        await self.session.close()
                    self.session = None
                else:
                    logger.error(f"Tutti i tentativi falliti per signature Vavoo: {str(e)}")
                    return None
        
        return None

    async def _resolve_vavoo_link(self, link: str, signature: str) -> Optional[str]:
        headers = {
            "user-agent": "MediaHubMX/2",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8", 
            "accept-encoding": "gzip",
            "mediahubmx-signature": signature
        }
        data = {
            "language": "de",
            "region": "AT", 
            "url": link,
            "clientVersion": "3.1.21"
        }
        
        try:
            logger.info(f"Tentativo di risoluzione URL Vavoo: {link}")
            session = await self._get_session()
            
            async with session.post(
                "https://vavoo.to/mediahubmx-resolve.json",
                json=data,
                headers=headers
            ) as resp:
                resp.raise_for_status()
                result = await resp.json()
                
                if isinstance(result, list) and result and result[0].get("url"):
                    resolved_url = result[0]["url"]
                    logger.info(f"URL Vavoo risolto con successo: {resolved_url}")
                    return resolved_url
                elif isinstance(result, dict) and result.get("url"):
                    resolved_url = result["url"]
                    logger.info(f"URL Vavoo risolto con successo: {resolved_url}")
                    return resolved_url
                else:
                    logger.warning(f"Nessun URL trovato nella risposta API Vavoo: {result}")
                    return None
        except Exception as e:
            logger.exception(f"Risoluzione Vavoo fallita per URL {link}: {str(e)}")
            return None

    async def extract(self, url: str) -> Dict[str, Any]:
        if "vavoo.to" not in url:
            raise ExtractorError("Non è un URL Vavoo valido")

        signature = await self.get_auth_signature()
        if not signature:
            raise ExtractorError("Fallito ottenere signature autenticazione Vavoo")

        resolved_url = await self._resolve_vavoo_link(url, signature)
        if not resolved_url:
            raise ExtractorError("Fallito risolvere URL Vavoo")

        stream_headers = {
            "user-agent": self.base_headers["user-agent"],
            "referer": "https://vavoo.to/",
        }

        return {
            "destination_url": resolved_url,
            "request_headers": stream_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
