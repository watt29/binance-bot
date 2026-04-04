import asyncio
import hmac
import hashlib
import time
import aiohttp # pyre-ignore
import urllib.parse
from typing import Dict, Any, Optional
from loguru import logger # pyre-ignore

class BinanceAsyncClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://fapi.binance.com", proxy: Optional[str] = None,
                 cf_worker_url: Optional[str] = None, cf_proxy_secret: Optional[str] = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.proxy = proxy  # Support for QuotaGuard or other Static IP Proxies

        # 🌐 Cloudflare Worker Proxy — มุด IP ผ่าน CF edge เมื่อ Binance block VPS IP
        # cf_worker_url  = "https://binance-proxy.regency2919.workers.dev/proxy"
        # cf_proxy_secret = "commander_proxy_secret_2026"
        self.cf_worker_url    = cf_worker_url.rstrip("/") if cf_worker_url else None
        self.cf_proxy_secret  = cf_proxy_secret
        self._use_cf_proxy    = bool(cf_worker_url)  # True = ส่งทุก request ผ่าน CF Worker

        self.session: Optional[aiohttp.ClientSession] = None
        self._backoff_until = 0.0
        self.used_weight_1m = 0
        self.weight_limit = 2400
        self._last_weight_update = 0.0

    async def __aenter__(self):
        if not self.session:
            headers: Dict[str, str] = {"X-MBX-APIKEY": self.api_key}
            if self._use_cf_proxy and self.cf_proxy_secret:
                headers["X-Proxy-Secret"] = self.cf_proxy_secret
            self.session = aiohttp.ClientSession(headers=headers)
        return self


    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close() # pyre-ignore
            self.session = None

    def is_backoff_active(self) -> bool:
        return time.time() < self._backoff_until

    def get_remaining_backoff(self) -> int:
        return max(0, int(self._backoff_until - time.time()))

    def _generate_signature(self, query_string: str) -> str:
        return hmac.new(self.api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

    async def _request(self, method: str, path: str, signed: bool = False, params: Optional[Dict[str, Any]] = None, priority: int = 2) -> Any:
        now = time.time()
        
        if now < self._backoff_until:
            wait_remaining = int(self._backoff_until - now)
            logger.warning(f"🔇 Rate limit back-off active. Skipping {path}. Waiting {wait_remaining}s...")
            return None
        
        usage_pct = (self.used_weight_1m / self.weight_limit) * 100 if self.weight_limit > 0 else 0
        
        if usage_pct > 95:
            logger.critical(f"🚨 CRITICAL WEIGHT ({usage_pct:.1f}%). IP is near ban limit! Blocking: {path}")
            return None
            
        elif usage_pct > 80:
            delay = 1.0 if priority == 0 else (2.0 if priority == 1 else 3.0)
            logger.warning(f"⚠️ HIGH IP USAGE ({usage_pct:.1f}%). Throttling: {path} (Delay {delay}s)")
            await asyncio.sleep(delay)

        elif usage_pct > 60:
            delay = 0.5 if priority == 0 else (1.0 if priority == 1 else 2.0)
            await asyncio.sleep(delay)

        session: aiohttp.ClientSession = self.session  # type: ignore[assignment]
        if session is None or session.closed:
            headers: Dict[str, str] = {"X-MBX-APIKEY": self.api_key}
            if self._use_cf_proxy and self.cf_proxy_secret:
                headers["X-Proxy-Secret"] = self.cf_proxy_secret
            session = aiohttp.ClientSession(headers=headers)
            self.session = session

        # 🌐 Cloudflare Worker Proxy: เปลี่ยน URL target ไปที่ CF Worker
        if self._use_cf_proxy:
            url = f"{self.cf_worker_url}{path}"
        else:
            url = f"{self.base_url}{path}"
        params = params or {}
        request_params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items()}
        
        for attempt in range(3):
            if signed:
                request_params["timestamp"] = int(time.time() * 1000)
                if "recvWindow" not in request_params: request_params["recvWindow"] = 10000
                request_params.pop("signature", None)
                query_string = urllib.parse.urlencode(request_params)
                request_params["signature"] = self._generate_signature(query_string)

            start_t = time.time()
            try:
                # 🛡️ Execute with Proxy support and Timeout
                async with session.request(method, url, params=request_params, timeout=15, proxy=self.proxy) as response:  # pyre-ignore
                    latency = (time.time() - start_t) * 1000
                    
                    weight_header = response.headers.get("X-MBX-USED-WEIGHT-1M")
                    if weight_header: 
                        self.used_weight_1m = int(weight_header)
                        self._last_weight_update = time.time()

                    data = await response.json()
                    
                    # 📊 Professional Latency Logging
                    if latency > 1000:
                        logger.warning(f"🐢 Slow Response: {latency:.0f}ms | {method} {path} | Weight: {self.used_weight_1m}")
                    else:
                        logger.debug(f"⚡ Latency: {latency:.0f}ms | {path}")

                    if response.status == 200:
                        return data
                    else:
                        logger.error(f"Binance API Error: {response.status} - {data} | URL: {url}")
                        if response.status in [429, 418]: 
                            retry_after = int(response.headers.get("Retry-After", 60))
                            if response.status == 418 and isinstance(data, dict) and 'msg' in data:
                                try:
                                    ts_str = data['msg'].split()[-1]
                                    wait_time = int((int(ts_str) / 1000.0) - time.time()) + 5 if ts_str.isdigit() else 300
                                except: wait_time = 300
                            else: wait_time = max(retry_after, 60)
                            
                            self._backoff_until = time.time() + wait_time
                            logger.critical(f"⛔ IP BAN/LIMIT! Waiting {wait_time}s. (Used Weight: {self.used_weight_1m}/{self.weight_limit})")
                            return None 
                        return data 
            except Exception as e:
                logger.warning(f"Attempt {attempt+1} failed: {e}")
                await asyncio.sleep(2)
        return None


    # --- Futures API Methods ---
    async def get_exchange_info(self):
        data = await self._request("GET", "/fapi/v1/exchangeInfo", priority=1)
        if data and isinstance(data, dict) and 'rateLimits' in data:
            for rl in data.get('rateLimits', []):
                if rl['rateLimitType'] == 'REQUEST_WEIGHT' and rl['interval'] == 'MINUTE':
                    self.weight_limit = int(rl['limit'])
                    logger.info(f"📊 Weight Limit updated: {self.weight_limit} per minute")
        return data

    async def get_order_book(self, symbol: str, limit: int = 20):
        """ดึง Order Book depth (Bids/Asks) — weight=2 สำหรับ limit<=20"""
        return await self._request("GET", "/fapi/v1/depth", params={"symbol": symbol, "limit": limit}, priority=2)

    async def get_ticker(self, symbol: str):
        return await self._request("GET", "/fapi/v1/ticker/price", params={"symbol": symbol}, priority=2)

    async def get_24h_stats(self, symbol: str):
        return await self._request("GET", "/fapi/v1/ticker/24hr", params={"symbol": symbol}, priority=2)

    async def get_mark_price(self, symbol: str):
        return await self._request("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol}, priority=2)

    async def get_account(self):
        return await self._request("GET", "/fapi/v2/account", signed=True, priority=1)

    async def get_position_risk(self, symbol: str):
        return await self._request("GET", "/fapi/v2/positionRisk", signed=True, params={"symbol": symbol}, priority=1)

    async def get_position_mode(self):
        return await self._request("GET", "/fapi/v1/positionSide/dual", signed=True, priority=1)

    async def get_income_history(self, symbol: str, startTime: int, endTime: int, limit: int = 1000):
        params = {"symbol": symbol, "startTime": startTime, "endTime": endTime, "limit": limit}
        return await self._request("GET", "/fapi/v1/income", signed=True, params=params, priority=2)

    async def change_position_mode(self, dualSidePosition: bool):
        return await self._request("POST", "/fapi/v1/positionSide/dual", signed=True, params={"dualSidePosition": str(dualSidePosition).lower()}, priority=0)

    async def change_margin_type(self, symbol: str, marginType: str):
        return await self._request("POST", "/fapi/v1/marginType", signed=True, params={"symbol": symbol, "marginType": marginType.upper()}, priority=0)

    async def change_leverage(self, symbol: str, leverage: int):
        return await self._request("POST", "/fapi/v1/leverage", signed=True, params={"symbol": symbol, "leverage": leverage}, priority=0)

    async def get_bnb_burn_status(self):
        """ตรวจสอบว่าเปิด BNB fee burn อยู่ไหม"""
        return await self._request("GET", "/sapi/v1/bnbBurn", signed=True, priority=1)

    async def set_bnb_burn(self, spot_bnb_burn: bool = True, interest_bnb_burn: bool = True):
        """เปิด BNB fee burn — spotBNBBurn=true ใช้ BNB จ่าย fee Spot/Futures"""
        params = {
            "spotBNBBurn": "true" if spot_bnb_burn else "false",
            "interestBNBBurn": "true" if interest_bnb_burn else "false",
        }
        return await self._request("POST", "/sapi/v1/bnbBurn", signed=True, params=params, priority=0)

    async def create_order(self, symbol: str, side: str, order_type: str, quantity: str, price: Optional[str] = None, **kwargs):
        params = {"symbol": symbol, "side": side.upper(), "type": order_type.upper(), "quantity": quantity}
        params.update(kwargs)
        if price:
            params["price"] = price
            if "timeInForce" not in params:
                params["timeInForce"] = "GTC"
        return await self._request("POST", "/fapi/v1/order", signed=True, params=params, priority=0)

    async def cancel_all_open_orders(self, symbol: str):
        """ยกเลิก Open Orders ทั้งหมดของ symbol — ใช้เมื่อ Kill Switch / Equity Lock เกิดขึ้น"""
        return await self._request("DELETE", "/fapi/v1/allOpenOrders", signed=True, params={"symbol": symbol}, priority=0)

    # --- User Data Stream ---
    async def futures_stream_get_listen_key(self):
        """Standard method name to create a new listenKey for User Data Stream."""
        data = await self._request("POST", "/fapi/v1/listenKey", priority=1)
        return data.get('listenKey') if data else None

    async def futures_stream_keepalive(self):
        """Standard method name to keep the listenKey alive."""
        return await self._request("PUT", "/fapi/v1/listenKey", priority=1)

    async def futures_stream_close(self):
        """Standard method name to close the listenKey."""
        return await self._request("DELETE", "/fapi/v1/listenKey", priority=1)

    # --- WebSocket Streaming ---
    async def stream_depth(self, symbol: str, callback):
        """WebSocket Order Book depth updates ทุก 500ms — ไม่ใช้ REST weight เลย"""
        url = f"wss://fstream.binance.com/ws/{symbol.lower()}@depth@500ms"
        await self._ws_loop(url, callback, f"Depth:{symbol}")

    async def stream_ticker(self, symbol: str, callback):
        url = f"wss://fstream.binance.com/ws/{symbol.lower()}@ticker"
        await self._ws_loop(url, callback, f"Ticker:{symbol}")

    async def stream_aggtrade(self, symbol: str, callback):
        """WebSocket Aggregated Trades stream — Executed trades ทุก 100ms
        ใช้คำนวณ Trade-based OBI (OBI^T) จากแรงซื้อ/ขายจริง"""
        url = f"wss://fstream.binance.com/ws/{symbol.lower()}@aggTrade"
        await self._ws_loop(url, callback, f"AggTrade:{symbol}")

    async def stream_user_data(self, listen_key: str, callback):
        url = f"wss://fstream.binance.com/ws/{listen_key}"
        await self._ws_loop(url, callback, "UserDataStream")

    async def _ws_loop(self, url, callback, label):
        while True:
            try:
                session: aiohttp.ClientSession = self.session  # type: ignore[assignment]
                if session is None or session.closed:
                    session = aiohttp.ClientSession(headers={"X-MBX-APIKEY": self.api_key})
                    self.session = session
                
                async with session.ws_connect(url) as ws:  # pyre-ignore
                    logger.info(f"Connected to WebSocket: {label}")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await callback(msg.json())
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except Exception as e:
                logger.error(f"WebSocket {label} Error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
