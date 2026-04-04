import os
import asyncio
import aiohttp  # pyre-ignore
from aiohttp import web  # pyre-ignore
import math
import time
import sys
from collections import deque
from loguru import logger  # pyre-ignore
from rich.console import Console  # pyre-ignore
from typing import Any, Optional, Dict, List
from datetime import datetime

# 🛡️ SYSTEM CONFIG & LOGGING
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from shared.config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GL_API_KEY, GL_API_SECRET, BINANCE_PROXY, CF_WORKER_URL, CF_PROXY_SECRET  # pyre-ignore
from binance_global.async_client import BinanceAsyncClient  # pyre-ignore

logger.remove()
logger.add(sys.stderr, level="INFO")
logger.add("logs/bot_commander.log", rotation="10 MB", retention="10 days", level="INFO")

console = Console()

def safe_float(val):
    try: return float(val) if val else 0.0
    except: return 0.0

class TelegramCommander:
    def __init__(self, bot: Any):
        self.token = TELEGRAM_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.bot = bot
        self.last_update_id = 0
        self.session: Optional[aiohttp.ClientSession] = None
        
        # 🧠 ADVANCED ANTI-SPAM: TOKEN BUCKET (IN-MEMORY)
        self._busy_lock = asyncio.Lock()
        self._user_buckets = {} # {user_id: {"tokens": 5, "last_refill": timestamp}}
        self._bucket_capacity = 5.0
        self._refill_rate = 0.5 # 1 token every 2 seconds

        # ป้องกันส่งซ้ำ
        self._last_user_cmd: Dict[int, Dict[str, Any]] = {} # {user_id: {"cmd": text, "time": timestamp}}

        # 📤 OUTBOUND RATE LIMITER — ป้องกัน Telegram API ban (Error 429)
        # Telegram limit: 1 msg/sec per chat, 20 msg/min per group
        # บอทส่งได้สูงสุด 1 msg/sec → Bucket capacity=3, refill=1/sec
        self._out_tokens: float = 3.0        # tokens ปัจจุบัน (burst สูงสุด 3 ข้อความ)
        self._out_last_refill: float = time.time()
        self._out_capacity: float = 3.0      # Bucket เต็มที่ 3 tokens
        self._out_refill_rate: float = 1.0   # เติม 1 token/วินาที (= 1 msg/sec)
        self._out_queue: deque = deque()      # คิวข้อความรอส่ง (สูงสุด 10 ข้อความ)
        self._out_queue_max: int = 10         # ทิ้งข้อความเก่าถ้าคิวเต็ม

    def _get_user_tokens(self, user_id: int) -> float:
        """คำนวณจำนวน Token ปัจจุบันของ User ตามหลัก Token Bucket"""
        now = time.time()
        if user_id not in self._user_buckets:
            self._user_buckets[user_id] = {"tokens": self._bucket_capacity, "last_refill": now}
            return self._bucket_capacity
        
        bucket = self._user_buckets[user_id]
        time_passed = now - bucket["last_refill"]
        new_tokens = min(self._bucket_capacity, bucket["tokens"] + (time_passed * self._refill_rate))
        
        bucket["tokens"] = new_tokens
        bucket["last_refill"] = now
        return new_tokens

    def _consume_out_token(self) -> bool:
        """ตรวจสอบและใช้ 1 outbound token — คืน True ถ้าส่งได้ทันที"""
        now = time.time()
        elapsed = now - self._out_last_refill
        self._out_tokens = min(self._out_capacity, self._out_tokens + elapsed * self._out_refill_rate)
        self._out_last_refill = now
        if self._out_tokens >= 1.0:
            self._out_tokens -= 1.0
            return True
        return False

    async def send_message(self, message: str, reply_markup: Optional[dict] = None):
        if not self.token: return

        # 📤 Outbound Token Bucket — ถ้า token หมด ใส่คิวก่อน (max 10 ข้อความ)
        if not self._consume_out_token():
            if len(self._out_queue) < self._out_queue_max:
                self._out_queue.append((message, reply_markup))
                logger.debug(f"📭 Telegram queued (tokens exhausted). Queue size: {len(self._out_queue)}")
            else:
                logger.warning(f"⚠️ Telegram queue full — dropped alert: {message[:60]}...")  # pyre-ignore
            return

        await self._send_raw(message, reply_markup)

        # ส่งคิวที่ค้างอยู่ทีละข้อความ (ถ้ามี token เหลือ)
        while self._out_queue and self._consume_out_token():
            queued_msg, queued_markup = self._out_queue.popleft()
            await self._send_raw(queued_msg, queued_markup)

    async def _send_raw(self, message: str, reply_markup: Optional[dict] = None):
        """ส่งข้อความจริงๆ ไปยัง Telegram API (ไม่มี rate limit ภายใน)"""
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}
        if reply_markup: payload["reply_markup"] = reply_markup
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:  # pyre-ignore
                    r = await resp.json()
                    if r.get("ok"):
                        logger.info(f"✅ Telegram Sent to {self.chat_id}")
                    elif r.get("error_code") == 429:
                        retry_after = r.get("parameters", {}).get("retry_after", 5)
                        logger.warning(f"⏳ Telegram 429 — backing off {retry_after}s")
                        await asyncio.sleep(retry_after)
                        await self._send_raw(message, reply_markup)  # retry once
                    else:
                        logger.error(f"Telegram API Send Error: {r} (Chat: {self.chat_id})")
        except Exception as e:
            logger.error(f"Telegram Connection Error: {e}")

    async def process_update(self, u: dict):
        """ประมวลผล Update จาก Telegram (ทั้ง Polling และ Webhook)"""
        try:
            msg = u.get("message", {})
            t = msg.get("text", "")
            from_user = msg.get("from", {})
            user_id = from_user.get("id")
            username = from_user.get("username", "Unknown")
            chat_id = msg.get("chat", {}).get("id")
            
            if not t or not user_id: return
            
            # 🛡️ TOKEN BUCKET CHECK (SAE Standard)
            tokens = self._get_user_tokens(user_id)
            if tokens < 1.0:
                # Token หมด! (Spam Detected)
                logger.warning(f"🚫 Spam Blocked: @{username} (Tokens: {tokens:.2f})")
                return # เงียบไว้เพื่อดัดนิสัยคนกดรัว
            
            # 🛡️ ป้องกันกดคำสั่งเดิมซ้ำรัวๆ (ภายใน 10 วินาที)
            now = time.time()
            last_cmd_info = self._last_user_cmd.get(user_id)
            
            # อัปเดตเวลาเพื่อให้หน้าต่าง spam รีเซ็ตใหม่ถ้าคนกดรัว
            self._last_user_cmd[user_id] = {"cmd": t, "time": now}
            
            if last_cmd_info and last_cmd_info["cmd"] == t and (now - last_cmd_info["time"]) < 10:
                logger.warning(f"🚫 Duplicate Blocked: @{username} sent '{t}' too fast (spam window reset)")
                return
            
            # 2. Global Busy Lock
            if self._busy_lock.locked():
                await self.send_message("⏳ *ระบบกำลังประมวลผลคำสั่งก่อนหน้า...*\nกรุณารอสักครู่ครับ")
                return

            # หัก Token 1 เหรียญสำหรับคำสั่งที่ได้รับอนุญาต
            self._user_buckets[user_id]["tokens"] -= 1.0

            logger.info(f"📩 Telegram Command: '{t}' from @{username} (Tokens Left: {self._user_buckets[user_id]['tokens']:.1f})")

            async with self._busy_lock:
                # ── ดูข้อมูล ──
                if t == "📊 พอร์ต" or t == "📊 เช็คพอร์ต":
                    await self.bot.send_combined_report()
                elif t == "💰 กำไรวันนี้":
                    await self.bot.send_trade_report()
                elif t == "📓 Flip Stats":
                    await self.send_message(self.bot._get_flip_stats())
                # ── เปลี่ยน Mode ──
                elif t in ("🛡️ SAFE", "🛡️ ขอปลอดภัยไว้ก่อน"):
                    self.bot.strategy_mode = "SAFE"
                    await self.bot.update_strategy_parameters()
                    await self.send_message("🛡️ *SAFE Mode:* ถอยระยะห่างไม้ +50% ลด TP")
                    await self.bot.send_combined_report()
                elif t in ("💸 PROFIT", "💸 ขอกำไรเข้าพอร์ตบ่อยๆ"):
                    self.bot.strategy_mode = "PROFIT"
                    await self.bot.update_strategy_parameters()
                    await self.send_message("💸 *PROFIT Mode:* ลด TP 50% ปิดงานเร็ว")
                    await self.bot.send_combined_report()
                elif t in ("🔄 NORMAL", "🔄 ปล่อยแบบเดิม"):
                    self.bot.strategy_mode = "NORMAL"
                    self.bot._equity_kill_active = False  # Reset Equity Kill Switch
                    await self.bot.update_strategy_parameters()
                    await self.send_message("🔄 *NORMAL Mode:* บอทตัดสินใจตามตลาดเอง\n✅ Equity Kill Switch ถูก Reset แล้วครับ")
                    await self.bot.send_combined_report()
                elif t == "🧮 วิเคราะห์ทางออก":
                    await self.bot.send_exit_analysis()
                elif t == "🔍 สแกนความเสี่ยง":
                    await self.bot.send_risk_scan()
                elif t == "/start":
                    await self._send_menu()

        except Exception as e:
            logger.error(f"Process Update Error: {e}")


    async def set_webhook(self, base_url: str):
        """ลงทะเบียน Webhook กับ Telegram API"""
        # 🛠️ ปรับแต่ง URL: ตัดโปรโตคอลออกหากมี เพื่อป้องกัน https://https://
        clean_url = base_url.replace("https://", "").replace("http://", "").strip("/")
        webhook_url = f"https://{clean_url}/{self.token}"

        url = f"https://api.telegram.org/bot{self.token}/setWebhook"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"url": webhook_url}) as resp:
                r = await resp.json()
                if r.get("ok"):
                    logger.info(f"🌐 Webhook successfully set to: {webhook_url}")
                    return True
                else:
                    logger.error(f"Failed to set webhook: {r} (URL: {webhook_url})")
                    return False

    async def poll_commands(self):
        """ระบบ Polling (ใช้สำหรับรัน Local เท่านั้น)"""
        if not self.session:
            self.session = aiohttp.ClientSession()
        # ลบ Webhook ออกก่อนเริ่ม Polling เพื่อป้องกัน Conflict
        await self.session.post(f"https://api.telegram.org/bot{self.token}/deleteWebhook")  # pyre-ignore
        logger.info("📡 Starting Telegram Polling (Local Mode)...")

        while True:
            try:
                url = f"https://api.telegram.org/bot{self.token}/getUpdates"
                params = {"offset": self.last_update_id + 1, "timeout": 20}
                async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=25)) as resp:  # pyre-ignore
                    r = await resp.json()
                    if r.get("ok"):
                        for u in r.get("result", []):
                            self.last_update_id = u["update_id"]
                            await self.process_update(u)
                    else:
                        if r.get("error_code") == 409:
                            logger.warning("⚠️ Conflict detected. Sleeping 30s to allow other instance...")
                            await asyncio.sleep(30)
                        else:
                            logger.error(f"Telegram API Error: {r}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Polling Error: {e}"); await asyncio.sleep(5)

    async def _send_menu(self):
        kb = {
            "keyboard": [
                # ── แถว 1: ดูข้อมูล ──
                [{"text": "📊 พอร์ต"}, {"text": "💰 กำไรวันนี้"}, {"text": "📓 Flip Stats"}],
                # ── แถว 2: เปลี่ยน Mode ──
                [{"text": "🛡️ SAFE"}, {"text": "💸 PROFIT"}, {"text": "🔄 NORMAL"}],
                # ── แถว 3: วิเคราะห์ ──
                [{"text": "🧮 วิเคราะห์ทางออก"}, {"text": "🔍 สแกนความเสี่ยง"}]
            ],
            "resize_keyboard": True
        }
        await self.send_message("🏰 *COMMANDER v2.0: Ready*\nเลือกคำสั่งได้เลยครับ!", reply_markup=kb)

class MainCommandCenter:
    def __init__(self):
        self.client_gl = BinanceAsyncClient(
            GL_API_KEY, GL_API_SECRET,
            proxy=BINANCE_PROXY,
            cf_worker_url=CF_WORKER_URL,
            cf_proxy_secret=CF_PROXY_SECRET,
        )
        if CF_WORKER_URL:
            logger.info(f"🌐 Cloudflare Worker Proxy ENABLED: {CF_WORKER_URL}")
        self.tg = TelegramCommander(self)
        self.symbol, self.gl_paused, self.leverage = "BTCUSDT", False, 15
        self.grid_step_pct, self.target_net_profit_pct = 0.5, 1.2
        self.strategy_mode = "NORMAL" # NORMAL, SAFE, PROFIT
        self.active_layers, self.trailing_active, self.peak_price = 0, False, 0.0
        self.last_close_time, self.last_buy_price = 0.0, 0.0
        self.current_price = 0.0
        self.price_buffer = [] 
        # ใช้ BNB จ่าย fee → ลด 25% | maker 0.02%→0.015% | taker 0.05%→0.0375%
        self.maker_fee, self.taker_fee = 0.00015, 0.000375
        self.min_qty, self.step_size, self.min_notional = 0.001, 0.001, 100.0
        self.p_prec, self.q_prec = 1, 3
        
        # 🧠 Cache & Rate Limiting
        self._cached_stats: Any = None
        self._last_stats_time = 0.0
        self._cached_acc: Any = None
        self._last_acc_time = 0.0
        self._last_report_time = 0.0


        # 📊 ORDER BOOK IMBALANCE (Whale Signal)
        self._obi_score = 0.0          # -1.0 ถึง +1.0  (flat weight, Top 20)
        self._obi_deep = 0.0           # -1.0 ถึง +1.0  (distance-weighted, Top 20)
        self._obi_bid_vol = 0.0
        self._obi_ask_vol = 0.0
        self._obi_last_update = 0.0
        self._whale_bid_walls: list = []   # กำแพงซื้อวาฬ [(price, qty), ...]
        self._whale_ask_walls: list = []   # กำแพงขายวาฬ [(price, qty), ...]

        # 📈 ORDER FLOW IMBALANCE (OFI) — ดูการเปลี่ยนแปลงจริง ไม่ใช่แค่ snapshot
        self._ofi_score = 0.0          # สะสม OFI -1 ถึง +1
        self._prev_best_bid: tuple = (0.0, 0.0)  # (price, qty) ก่อนหน้า
        self._prev_best_ask: tuple = (0.0, 0.0)
        self._ofi_buffer: list = []    # เก็บ OFI 20 ค่าล่าสุด (smoothing)

        # 🕵️ SPOOF DETECTOR — ตรวจกำแพงปลอม
        self._wall_history: dict = {}  # {price: {"qty": float, "seen": int, "gone": int}}
        self._spoof_prices: set = set()  # ราคาที่ถูกตัดสินว่า spoof

        # 🚨 OBI FLIP ALERT — ตรวจจับการชักสภาพคล่องกะทันหัน
        self._obi_buffer: list = []        # เก็บ OBI 10 ค่าล่าสุด (เพื่อเทียบ delta)
        self._obi_flip_alerted: float = 0.0  # timestamp ที่แจ้งเตือนล่าสุด (cooldown)

        # 💹 TRADE-BASED OBI (OBI^T) — วัดจาก Executed Trades จริง ไม่ใช่ Limit Order
        # ป้องกัน Flickering Liquidity / Spoofing ใน Choppy Market ได้ 100%
        self._trade_history: deque = deque()  # (timestamp, side, volume)
        self._trade_buy_vol: float = 0.0      # Running buy volume (30s window)
        self._trade_sell_vol: float = 0.0     # Running sell volume (30s window)
        self._trade_obi: float = 0.0          # Trade OBI: -1.0 (sell) ถึง +1.0 (buy)
        self.TRADE_OBI_WINDOW: float = 30.0   # Time window (วินาที)

        # 📓 FLIP LOGGER (Priority 4: Self-Learning) — บันทึกทุก OBI Flip เพื่อสร้าง Pattern
        # เก็บสูงสุด 100 events ล่าสุด (deque auto-evict เก่าออก)
        self._flip_log: deque = deque(maxlen=100)
        # {time, obi_before, obi_after, trade_obi, ofi, price, regime, cooldown,
        #  outcome_price (ราคา 5 นาทีหลัง), outcome_delta (%), outcome_label}
        self._flip_pending_outcome: list = []  # รอ record outcome หลัง 5 นาที
        self._flip_log_path = "logs/flip_log.json"  # persist ข้ามรอบ restart
        self._trailing_state_path = "logs/trailing_state.json"  # persist trailing state

        # 📚 LOCAL ORDER BOOK (Binance standard sync)
        self._lob_bids: dict = {}        # {price: qty}
        self._lob_asks: dict = {}        # {price: qty}
        self._lob_last_update_id = 0
        self._lob_prev_u = 0
        self._lob_buffer: list = []
        self._lob_ready = False          # True เมื่อ sync สำเร็จ

        # 🔴 KILL SWITCH — หยุดฉุกเฉิน
        self._kill_switch = False        # True = หยุดเทรดทั้งหมด
        self._kill_reason = ""
        self._kill_time = 0.0
        self._vol_spike_count = 0        # นับ volatility spike ติดต่อกัน
        # เงื่อนไข kill switch — ปรับให้เหมาะกับพอร์ตเล็ก margin ~$300
        self.KS_VOL_THRESHOLD  = 1.0    # inst_vol > 1.0% = volatile (ลดจาก 1.5)
        self.KS_VOL_SPIKE_MAX  = 2      # spike 2 รอบติดกัน = trigger (ไวขึ้น)
        self.KS_COOLDOWN_SEC   = 60     # Base cooldown (Dynamic Regime-Aware จะปรับอัตโนมัติ)
        # 🧭 DYNAMIC COOLDOWN — ปรับตาม Regime อัตโนมัติ (Priority 2 Active)
        self.KS_COOLDOWN_CHOPPY   = 60   # CHOPPY: Liquidity กลับเร็ว
        self.KS_COOLDOWN_TRENDING = 300  # TRENDING: รอ Momentum หมดแรงก่อน (เพิ่มจาก 120→300s)
        self.KS_COOLDOWN_VOLATILE = 240  # VOLATILE: Capital Preservation สูงสุด
        self.KS_COOLDOWN_WARMING  = 60   # WARMING (buffer ยังน้อย): ใช้ค่าเดิม

        # 🧭 DYNAMIC REGIME & CUSUM VARS
        self._cusum_pos = 0.0
        self._cusum_neg = 0.0
        self._safe_bar_count = 0
        self.CUSUM_K = 1.5
        self.CUSUM_H = 5.0
        self._cooldown_state = "READY"

        # ⚖️ INVENTORY SKEW CONTROL — พอร์ตใกล้ Liq.
        self.INV_MAX_LAYERS    = 6      # ลดจาก 8 → หยุด DCA เร็วขึ้น
        self.INV_MAX_LOSS_PCT  = 40.0   # ขาดทุน > 40% margin = toxic (ปรับตามสภาพจริง)

        # 🛡️ EQUITY KILL SWITCH — หยุดเปิดไม้ใหม่เมื่อ Drawdown เกินเกณฑ์
        self.EQUITY_DRAWDOWN_LIMIT = -0.30  # PNL% ของ Balance ติดลบเกิน 30% → จำศีล
        self._equity_kill_active = False     # True = ล็อค DCA จนกว่า User จะ Reset
        self.EMERGENCY_BAL_LIMIT = 30.0      # Available < $30 → Block DCA ทันที

        # 📐 DYNAMIC LOT SIZING — ลด Lot ตาม Layer (Downward Protection)
        # Layer 1-5: 100% | 6-7: 75% | 8-9: 50% | 10+: 30%
        self.LOT_SCALE_BY_LAYER = {0: 1.0, 6: 0.75, 8: 0.50, 10: 0.30}

        # 🌡️ VOLATILITY GATE (σ/μ Ratio) — ตรวจ Regime จาก price buffer
        # σ/μ > 5.0 = CHOPPY (ปลอดภัย) | < 5.0 = TRENDING (อันตราย)
        self.VGATE_SIGMA_MU_MIN = 5.0   # เกณฑ์ขั้นต่ำ
        self.VGATE_REARM_BARS  = 3      # ต้องผ่าน σ/μ > 5.0 ติดต่อกัน 3 รอบ ถึง Re-arm
        self._vgate_safe_count = 0      # นับรอบที่ปลอดภัยติดต่อกัน
        self._vgate_blocked    = False  # True = VolatilityGate บล็อก DCA ไว้
        self._vgate_price_history: list = []  # เก็บ price สำหรับคำนวณ MA drift

        # ⏳ VARIANCE AGE EXIT — บังคับปิด Position หากถือนานเกิน 72 ชั่วโมง
        self.AGE_EXIT_HOURS   = 72      # ชั่วโมงสูงสุดที่ถือ Position ไว้
        self._position_open_time = 0.0  # timestamp ที่เปิด Position

        # 📅 MAX DAILY LOSS — หยุดเทรดทั้งวันเมื่อขาดทุนสะสมถึงเกณฑ์ (Prop Firm style)
        self.MAX_DAILY_LOSS    = -50.0  # ขาดทุนสะสม > $50/วัน → หยุดทั้งวัน
        self._daily_loss_total = 0.0    # สะสม PnL ที่ปิดแล้ว (Realized) วันนี้
        self._daily_loss_date  = ""     # วันที่ล่าสุดที่ reset (YYYY-MM-DD)
        self._daily_kill_active = False # True = หยุดเทรดจนถึงเที่ยงคืน

        # ⚡ RAPID-FIRE ORDER THROTTLE — บล็อกเมื่อส่ง order มากเกินในเวลาสั้น
        self.THROTTLE_MAX_ORDERS = 3    # สูงสุด 3 orders ใน 60 วินาที
        self.THROTTLE_WINDOW_SEC = 60   # time window
        self._order_timestamps: list = []  # เก็บ timestamp ของ orders ล่าสุด
        self._throttle_blocked  = False # True = ถูกบล็อก

        # 🤖 AUTO-MONITOR (Cipher Logic Built-in)
        self._monitor_interval = 60        # เช็คทุก 1 นาที (วิกฤต — ลด Liq. ใกล้)
        self._monitor_last_check = 0.0
        self._monitor_last_alert: dict = {"type": None, "pnl": 0.0, "layers": 0}
        # กฎตัดสินใจ — ปรับตามพอร์ตจริง entry $68,510 / margin $328
        self.MONITOR_PROFIT_TARGET  =  5.0   # กำไร +$5 ก็ปิดเลย (ลดจาก $8)
        self.MONITOR_MAX_LOSS       = -120.0 # เตือนที่ -$120 (ปรับตามขาดทุนจริง $152)
        self.MONITOR_CRITICAL_LAYERS = 8     # ลดจาก 10 → บังคับ SAFE เร็วขึ้น
        self.MONITOR_HIGH_LAYERS     = 6     # ลดจาก 8
        self.MONITOR_NEAR_PROFIT     =  2.0  # กำไร +$2 → เปลี่ยน PROFIT mode
        self.listen_key: Any = None
        
        # Predictive Variables
        self.next_buy_price = 0.0
        self.predicted_avg_price = 0.0

        # 💰 MAKER REBATE TRACKING — บันทึกค่า fee ที่ประหยัดได้สะสม (GTX Maker vs Taker baseline)
        # rebate per trade = (taker_fee - maker_fee) * notional
        self._rebate_saved_total: float = 0.0   # สะสมตลอดอายุบอท (USDT)
        self._rebate_session_start: float = time.time()  # เวลาเริ่ม session
        self._rebate_trade_count: int = 0        # จำนวน Maker trades ที่สำเร็จ

        # 🚫 OBI QUOTE CANCELLATION — เก็บ pending GTX order สำหรับตรวจ OBI หลังวาง
        # เมื่อ OBI พลิกเสียเปรียบก่อน fill → cancel ทันที (ป้องกัน Adverse Selection)
        self._pending_gtx_order: Optional[dict] = None  # {"orderId": str, "price": float, "placed_at": float}
        self._gtx_cancel_obi_threshold: float = -0.45   # OBI ต่ำกว่านี้ → cancel
        self._gtx_cancel_checked = False                 # ป้องกัน cancel loop

        # 🔔 PRICE LEVEL ALERTS — แจ้งเตือนเมื่อราคาหลุดแนวรับสำคัญ
        # ตั้งค่าได้ตลอด: list of (label, price, direction)
        # direction: "below" = แจ้งเมื่อราคา < price, "above" = แจ้งเมื่อราคา > price
        self.PRICE_ALERT_LEVELS: list = [
            ("แนวรับ 1",  65938.0, "below"),
            ("แนวรับ 2 ⚠️ LIQ ZONE", 64197.0, "below"),
            ("แนวรับ 3",  62979.0, "below"),
            ("แนวรับ 4",  61721.0, "below"),
        ]
        self._price_alert_triggered: set = set()  # label ที่แจ้งแล้ว (reset เมื่อราคาฟื้น)
        self._price_alert_cooldown = 300.0         # cooldown วินาที (5 นาที)
        self._price_alert_last_time: dict = {}     # {label: timestamp}

    async def run(self):
        if not os.path.exists("logs"): os.makedirs("logs")
        self._flip_log_load()
        self._trailing_state_load()
        async with self.client_gl as client:
            success = await self._init_setup(client)
            if not success:
                logger.error("❌ SETUP FAILED. Bot will continue but may have issues.")
            
            console.print("[bold green][OK] COMMANDER V119.9 OPERATIONAL (PREDICTIVE ASYNC ENGINE)[/bold green]")
            
            # Start Background Tasks
            tasks = [
                self.trading_engine(client),
                self.client_gl.stream_ticker(self.symbol, self.price_update_callback),
                self.client_gl.stream_depth(self.symbol, self.depth_update_callback),
                self.client_gl.stream_aggtrade(self.symbol, self.aggtrade_callback),
                self.listen_key_keepalive(),
                self.hourly_alert_task()
            ]
            
            if self.listen_key:
                tasks.append(self.client_gl.stream_user_data(self.listen_key, self.user_data_callback))
            
            await asyncio.gather(*tasks)

    async def aggtrade_callback(self, data):
        """รับข้อมูล Executed Trades จาก <symbol>@aggTrade stream ทุก 100ms
        ใช้สร้าง Trade-based OBI (OBI^T) — วัดแรงซื้อ/ขายจริงที่จับคู่สำเร็จแล้ว
        """
        try:
            vol = float(data.get('q', 0))
            if vol <= 0:
                return
            # m = is_buyer_maker: True → Maker ฝั่งซื้อ → Taker เป็นฝั่งขาย = Market Sell
            is_buyer_maker = data.get('m', False)
            side = 'SELL' if is_buyer_maker else 'BUY'
            self._update_trade_obi(side, vol)
        except Exception as e:
            logger.debug(f"AggTrade Callback Error: {e}")

    def _update_trade_obi(self, side: str, vol: float):
        """คำนวณ Trade OBI แบบ Rolling Window 30s — O(1) per update"""
        now = time.time()

        # Push ข้อมูลใหม่
        self._trade_history.append((now, side, vol))
        if side == 'BUY':
            self._trade_buy_vol += vol
        else:
            self._trade_sell_vol += vol

        # Pop ข้อมูลเก่าที่อายุเกิน window
        while self._trade_history and (now - self._trade_history[0][0]) > self.TRADE_OBI_WINDOW:
            old_ts, old_side, old_vol = self._trade_history.popleft()
            if old_side == 'BUY':
                self._trade_buy_vol = max(0.0, self._trade_buy_vol - old_vol)
            else:
                self._trade_sell_vol = max(0.0, self._trade_sell_vol - old_vol)

        # คำนวณ OBI^T = (Buy - Sell) / (Buy + Sell)
        total = self._trade_buy_vol + self._trade_sell_vol
        self._trade_obi = (self._trade_buy_vol - self._trade_sell_vol) / total if total > 0 else 0.0

    async def depth_update_callback(self, data):
        """รับ Order Book depth จาก WebSocket ทุก 500ms
        0. LOB sync (Binance standard)
        1. OBI Weighted (L=20)
        2. OFI — ดูการเปลี่ยนแปลง best bid/ask จริง
        3. Spoof Detector — กำแพงที่หายเร็ว = ปลอม
        """
        await self._on_depth_event(data)  # 0. LOB sync
        try:
            # ใช้ข้อมูลจาก Local Order Book (ที่ sync แล้ว) แทน raw event
            # เพราะ depth event เป็น diff — ราคาใน event อาจไม่ครบหรือผิดปกติ
            if self._lob_ready and self._lob_bids and self._lob_asks:
                cur_p_ref = self.current_price if self.current_price > 0 else 60000.0
                # กรองเฉพาะราคาที่สมเหตุผล (±10% จากราคาปัจจุบัน)
                min_p, max_p = cur_p_ref * 0.90, cur_p_ref * 1.10
                bids_f = sorted(
                    [(p, q) for p, q in self._lob_bids.items() if min_p <= p <= max_p],
                    key=lambda x: -x[0]
                )[:20]  # pyre-ignore
                asks_f = sorted(
                    [(p, q) for p, q in self._lob_asks.items() if min_p <= p <= max_p],
                    key=lambda x: x[0]
                )[:20]  # pyre-ignore
            else:
                # fallback: ใช้ raw event แต่กรองราคาผิดปกติออก
                cur_p_ref = self.current_price if self.current_price > 0 else 60000.0
                min_p, max_p = cur_p_ref * 0.90, cur_p_ref * 1.10
                bids_raw = data.get('b', [])
                asks_raw = data.get('a', [])
                bids_f = [(float(p), float(q)) for p, q in bids_raw if min_p <= float(p) <= max_p and float(q) > 0][:20]  # pyre-ignore
                asks_f = [(float(p), float(q)) for p, q in asks_raw if min_p <= float(p) <= max_p and float(q) > 0][:20]  # pyre-ignore

            if not bids_f or not asks_f:
                return

            # ── 1. OBI Weighted ──────────────────────────────────
            bid_val = sum(p * q for p, q in bids_f)
            ask_val = sum(p * q for p, q in asks_f)
            total = bid_val + ask_val
            if total <= 0:
                return
            prev_obi = self._obi_score
            self._obi_score = (bid_val - ask_val) / total
            self._obi_bid_vol = bid_val
            self._obi_ask_vol = ask_val
            self._obi_last_update = time.time()

            # ── 1b. OBI Deep (Top 5 + Distance-Weighted) ─────────
            # ใช้เฉพาะ Top 5 levels — จุดสมดุล depth/noise ที่ดีที่สุด
            # w_i = 1 / (1 + d_i)  โดย d_i = |price_i - mid| / tick_size (tick distance)
            # tick_size BTC/USDT Futures = $0.10
            mid_p = (bids_f[0][0] + asks_f[0][0]) / 2 if bids_f and asks_f else cur_p_ref
            if mid_p > 0:
                tick_size = 0.10
                bids_top5 = bids_f[:5]
                asks_top5 = asks_f[:5]
                w_bid = sum(q / (1.0 + abs(p - mid_p) / tick_size) for p, q in bids_top5)
                w_ask = sum(q / (1.0 + abs(p - mid_p) / tick_size) for p, q in asks_top5)
                w_total = w_bid + w_ask
                self._obi_deep = (w_bid - w_ask) / w_total if w_total > 0 else 0.0

            # ── OBI Flip Detection (Filtered) ────────────────────────────────
            self._obi_buffer.append(self._obi_score)
            if len(self._obi_buffer) > 30:  # เพิ่มเป็น 30 ticks (ดูย้อนหลัง 15-30 วินาที กรอง Noise)
                self._obi_buffer.pop(0)
            
            # คำนวณ Smooth OBI (SMA 5) ตัดสัญญาณกำแพงปลอมที่มาแค่ 1-2 วินาที
            obi_sma = sum(self._obi_buffer[-5:]) / len(self._obi_buffer[-5:]) if self._obi_buffer else self._obi_score  # pyre-ignore
            obi_recent_max = max(self._obi_buffer) if self._obi_buffer else 0.0
            
            flip_cooldown = 120.0  # Alert cooldown (2 นาที)
            now_flip = time.time()
            
            # เงื่อนไข Flip ใหม่ที่แม่นยำขึ้น
            # 1. ร่วงมาติดลบต่อเนื่อง (SMA < -0.15) ไม่ใช่แค่ tick เดียว
            # 2. เคยสูงเกิน +0.60
            # 3. OFI ยืนยันแรงขายจาก best bid/ask delta (OFI < -0.20)
            # 4. [NEW] Trade OBI ยืนยัน Market Sell จริง (OBI^T < -0.15) — Ultimate Confirm
            #    ถ้า Trade OBI ยังเป็นกลาง → เป็นแค่ Flickering Liquidity → ข้ามไป
            trade_obi_confirms_sell = self._trade_obi < -0.15
            # Diagnostics: log เมื่อ Flip conditions 1-3 ผ่าน แต่ Trade OBI block
            if (obi_sma < -0.15 and obi_recent_max >= 0.60
                    and self._obi_score < -0.30 and self._ofi_score < -0.20
                    and not trade_obi_confirms_sell
                    and (now_flip - self._obi_flip_alerted) > flip_cooldown):
                _r, _rng = self._get_regime()
                logger.debug(f"🟡 OBI FLIP BLOCKED by TradeOBI: {self._trade_obi:+.2f} (Regime {_r} {_rng:.2f}%) — Flickering Liquidity ignored")

            if (obi_sma < -0.15
                    and obi_recent_max >= 0.60
                    and self._obi_score < -0.30
                    and self._ofi_score < -0.20
                    and trade_obi_confirms_sell
                    and not self._kill_switch
                    and (now_flip - self._obi_flip_alerted) > flip_cooldown):
                self._obi_flip_alerted = now_flip
                _flip_cd = self._get_dynamic_cooldown()
                _flip_regime, _ = self._get_regime()

                # 📊 CHOPPY BYPASS: OBI Flip ใน CHOPPY = Mechanical rebalance ไม่ใช่ Informational signal
                # Data (n=72): FLAT 60% / BOUNCE 22% / DUMP 18% → Kill Switch ทำให้เสียโอกาส Grid/DCA โดยไม่จำเป็น
                # Volatility Spike Kill Switch ยังทำงานปกติทุก Regime
                _ks_triggered = False
                if _flip_regime == "CHOPPY":
                    logger.info(f"🟡 OBI FLIP BYPASSED (CHOPPY): {obi_recent_max:+.2f} → {self._obi_score:+.2f} | TradeOBI {self._trade_obi:+.2f} — Grid continues")
                else:
                    asyncio.ensure_future(self.tg.send_message(
                        f"🚨 *OBI FLIP ALERT (Triple Verified)!*\n"
                        f"OBI ร่วงจาก `{obi_recent_max:+.2f}` → `{self._obi_score:+.2f}`\n"
                        f"📉 OFI: `{self._ofi_score:+.2f}` | Trade OBI: `{self._trade_obi:+.2f}`\n"
                        f"🧭 Regime: `{_flip_regime}` → ชะลอระบบ `{_flip_cd}s` (แรงขายจริง ไม่ใช่ Spoof)"
                    ))
                    self._kill_switch = True
                    self._kill_time = now_flip  # Dynamic cooldown จะคำนวณใน _check_kill_switch
                    self._kill_reason = f"Triple Verified Flip (OFI {self._ofi_score:+.2f} / TradeOBI {self._trade_obi:+.2f})"
                    _ks_triggered = True
                    _r, _rng = self._get_regime()
                    logger.warning(f"🚨 OBI FLIP TRIPLE-VERIFIED: {obi_recent_max:+.2f} → {self._obi_score:+.2f} | TradeOBI {self._trade_obi:+.2f} | Regime {_r}({_rng:.2f}%) | Cooldown {_flip_cd}s")

                # 📓 FLIP LOGGER — บันทึก event นี้ และ schedule outcome check ใน 5 นาที
                _flip_entry = {
                    "time": now_flip,
                    "obi_before": round(obi_recent_max, 3),  # pyre-ignore
                    "obi_after": round(self._obi_score, 3),  # pyre-ignore
                    "trade_obi": round(self._trade_obi, 3),  # pyre-ignore
                    "ofi": round(self._ofi_score, 3),  # pyre-ignore
                    "price": self.current_price,
                    "regime": _flip_regime,
                    "cooldown": _flip_cd,
                    "ks_triggered": _ks_triggered,  # True = Kill Switch fired, False = CHOPPY bypass
                    "outcome_price": None,
                    "outcome_delta": None,
                    "outcome_label": None,
                }
                self._flip_log.append(_flip_entry)
                # Schedule outcome ใน 300 วินาที (5 นาที)
                self._flip_pending_outcome.append({
                    "entry_ref": _flip_entry,
                    "check_at": now_flip + 300,
                    "entry_price": self.current_price,
                })

            # ── 2. OFI (Order Flow Imbalance) ────────────────────
            # OFI = +1 buy dominates, -1 sell dominates
            # คำนวณจาก sign ของ delta bid/ask แต่ละ tick (normalized)
            cur_bid = bids_f[0] if bids_f else (0.0, 0.0)
            cur_ask = asks_f[0] if asks_f else (0.0, 0.0)
            pb_p, pb_q = self._prev_best_bid
            pa_p, pa_q = self._prev_best_ask

            ofi_val = 0.0
            if pb_p > 0 and pa_p > 0:
                # bid delta: ราคาหรือ qty เพิ่ม = buy pressure (+1), ลด = sell pressure (-1)
                if cur_bid[0] > pb_p or (cur_bid[0] == pb_p and cur_bid[1] > pb_q):
                    ofi_val += 1.0
                elif cur_bid[0] < pb_p or (cur_bid[0] == pb_p and cur_bid[1] < pb_q):
                    ofi_val -= 1.0
                # ask delta: ราคาหรือ qty ลด = buy pressure (+1), เพิ่ม = sell pressure (-1)
                if cur_ask[0] < pa_p or (cur_ask[0] == pa_p and cur_ask[1] < pa_q):
                    ofi_val += 1.0
                elif cur_ask[0] > pa_p or (cur_ask[0] == pa_p and cur_ask[1] > pa_q):
                    ofi_val -= 1.0

            self._prev_best_bid = cur_bid
            self._prev_best_ask = cur_ask

            # smooth OFI ด้วย buffer 30 ค่า → normalize -1 ถึง +1
            self._ofi_buffer.append(ofi_val)
            if len(self._ofi_buffer) > 30:
                self._ofi_buffer.pop(0)
            buf_len = len(self._ofi_buffer)
            self._ofi_score = sum(self._ofi_buffer) / (buf_len * 2.0) if buf_len > 0 else 0.0

            # ── 3. Spoof Detector (3 ด่าน) ───────────────────────
            mid_price = (bids_f[0][0] + asks_f[0][0]) / 2 if bids_f and asks_f else self.current_price

            # ด่าน 1: Volume >= 6x avg (Slippage Power — ลด false positive)
            all_qtys = [q for _, q in bids_f] + [q for _, q in asks_f]
            avg_qty = sum(all_qtys) / len(all_qtys) if all_qtys else 1.0
            whale_threshold = avg_qty * 6.0   # 6x = institutional minimum
            mega_threshold  = avg_qty * 15.0  # 15x = S-class (OTC level)
            strong_threshold = avg_qty * 8.0  # 8x = strong institutional

            # ด่าน 2: ระยะห่างจาก mid ไม่เกิน 0.5% (ไกลกว่า = Bait Wall)
            max_dist_pct = 0.005

            now_t = time.time()
            current_walls = set()
            whale_bids, whale_asks = [], []

            for p, q in bids_f + asks_f:
                dist_pct = abs(p - mid_price) / mid_price if mid_price > 0 else 1.0  # pyre-ignore

                # ด่าน 1 + ด่าน 2
                if q < whale_threshold or dist_pct > max_dist_pct:
                    continue

                price_key = int(p / 10) * 10  # round ทีละ $10
                current_walls.add(price_key)

                if price_key not in self._wall_history:
                    self._wall_history[price_key] = {
                        "qty": q, "seen": 1, "gone": 0,
                        "first_seen": now_t, "last_seen": now_t
                    }
                else:
                    h = self._wall_history[price_key]
                    h["seen"] += 1
                    h["gone"] = 0
                    h["last_seen"] = now_t
                    # ตรวจ Wall Migration: ราคาขยับตาม mid เกิน $20 = spoof
                    if abs(price_key - mid_price) < 20 and h["seen"] <= 3:  # pyre-ignore
                        self._spoof_prices.add(price_key)

            # ด่าน 3: Time threshold — ต้องอยู่นานกว่า 3 วินาที (= seen >= 6 รอบ @ 500ms)
            for price_key in list(self._wall_history.keys()):
                h = self._wall_history[price_key]
                if price_key not in current_walls:
                    h["gone"] += 1
                    duration = h["last_seen"] - h["first_seen"]
                    # spoof = หายเร็ว + อยู่ไม่ถึง 3 วินาที
                    if h["gone"] >= 2 and duration < 3.0:
                        self._spoof_prices.add(price_key)
                        logger.debug(f"🕵️ Spoof @ ${price_key:,.0f} dur={duration:.1f}s seen={h['seen']}")
                    if h["gone"] > 24:  # ลืมหลัง 12 วินาที
                        self._wall_history.pop(price_key, None)
                        self._spoof_prices.discard(price_key)

            # รวม whale walls ที่ผ่านทุกด่าน + บันทึก tier
            def get_tier(q):
                if q >= mega_threshold:   return "mega"
                if q >= strong_threshold: return "strong"
                return "watch"

            for p, q in bids_f:
                price_key = int(p / 10) * 10
                h = self._wall_history.get(price_key, {})
                duration = h.get("last_seen", now_t) - h.get("first_seen", now_t)
                if (q >= whale_threshold
                        and abs(p - mid_price) / mid_price <= max_dist_pct  # pyre-ignore
                        and duration >= 3.0
                        and price_key not in self._spoof_prices):
                    whale_bids.append((p, q, get_tier(q)))
            for p, q in asks_f:
                price_key = int(p / 10) * 10
                h = self._wall_history.get(price_key, {})
                duration = h.get("last_seen", now_t) - h.get("first_seen", now_t)
                if (q >= whale_threshold
                        and abs(p - mid_price) / mid_price <= max_dist_pct  # pyre-ignore
                        and duration >= 3.0
                        and price_key not in self._spoof_prices):
                    whale_asks.append((p, q, get_tier(q)))

            self._whale_bid_walls = whale_bids
            self._whale_ask_walls = whale_asks

        except Exception as e:
            logger.debug(f"Depth Callback Error: {e}")

    async def price_update_callback(self, data):
        """รับราคา Real-time จาก WebSocket"""
        self.current_price = safe_float(data.get('c'))
        # 🟢 Optimization: Update 24h stats from WebSocket to save REST API weight
        high = safe_float(data.get('h'))
        low = safe_float(data.get('l'))
        if high > 0 and low > 0:
            self._cached_stats = {
                'highPrice': str(high),
                'lowPrice': str(low),
                'lastPrice': data.get('c'),
                'symbol': data.get('s')
            }
            self._last_stats_time = time.time()

    async def user_data_callback(self, data):
        """รับข้อมูลบัญชี/ออเดอร์ Real-time จาก User Data Stream"""
        try:
            event = data.get('e')
            if event == 'ACCOUNT_UPDATE':
                # Update Cache from WebSocket data
                if not self._cached_acc: self._cached_acc = {'assets': [], 'positions': []}
                
                upd = data.get('a', {})
                # Update Balances
                for b in upd.get('B', []):
                    asset = b.get('a')
                    target = next((a for a in self._cached_acc['assets'] if a['asset'] == asset), None)
                    if target:
                        target['walletBalance'] = b.get('wb')
                        target['availableBalance'] = b.get('cw')
                    else:
                        self._cached_acc['assets'].append({'asset': asset, 'walletBalance': b.get('wb'), 'availableBalance': b.get('cw')})
                
                # Update Positions
                for p in upd.get('P', []):
                    sym = p.get('s')
                    target = next((pos for pos in self._cached_acc['positions'] if pos['symbol'] == sym), None)
                    if target:
                        target['positionAmt'] = p.get('pa')
                        target['entryPrice'] = p.get('ep')
                        target['unrealizedProfit'] = p.get('up')
                    else:
                        self._cached_acc['positions'].append({'symbol': sym, 'positionAmt': p.get('pa'), 'entryPrice': p.get('ep'), 'unrealizedProfit': p.get('up')})
                
                self._last_acc_time = time.time()
                logger.debug(f"🔄 Account Updated via WebSocket ({event})")

            elif event == 'ORDER_TRADE_UPDATE':
                # Force refresh account on trade execution to be sure
                logger.info(f"⚡ Order/Trade Update: {data.get('o', {}).get('x')} - {data.get('o', {}).get('X')}")
                await self._get_cached_account(force=True)
                
        except Exception as e:
            logger.error(f"User Data Callback Error: {e}")

    async def listen_key_keepalive(self):
        """ส่ง Keep-alive สำหรับ listenKey ทุกๆ 30 นาที"""
        while True:
            try:
                await asyncio.sleep(1800) # 30 mins
                if self.listen_key:
                    await self.client_gl.futures_stream_keepalive()
                    logger.info("🔑 ListenKey Keep-alive sent.")
            except Exception as e:
                logger.error(f"Keep-alive Error: {e}")

    async def _get_cached_account(self, force: bool = False) -> Optional[Dict]:
        """ดึงข้อมูล Account แบบมี Cache (ขยายเวลาเป็น 5 นาทีหากใช้ WebSocket)"""
        now = time.time()
        # ถ้ามี WebSocket ให้ขยายเวลา Cache เป็น 5 นาที (เผื่อไว้เป็น Fallback)
        cache_duration = 300 if self.listen_key else 15
        
        if force or not self._cached_acc or (now - self._last_acc_time) > cache_duration:
            try:
                # 🛡️ Add timeout to prevent engine hanging
                acc = await asyncio.wait_for(self.client_gl.get_account(), timeout=10)
                if acc and 'assets' in acc:
                    self._cached_acc = acc
                    self._last_acc_time = now
            except Exception as e:
                logger.warning(f"⚠️ Account Fetch Timeout/Error: {e}")
            return self._cached_acc
        return self._cached_acc

    async def _get_cached_stats(self, force=False):
        """ดึงข้อมูล 24h Stats แบบมี Cache (เน้นใช้ WebSocket)"""
        now = time.time()
        # 🟢 Optimization: ถ้ามีข้อมูลจาก WS และยังไม่เก่าเกิน 5 นาที ให้ใช้ของเดิม ไม่ต้องยิง REST
        if not force and self._cached_stats and (now - self._last_stats_time) < 300:
            return self._cached_stats

        if force or not self._cached_stats or (now - self._last_stats_time) > 60:
            stats_r = await self.client_gl.get_24h_stats(self.symbol)
            if stats_r and 'highPrice' in stats_r:
                self._cached_stats = stats_r
                self._last_stats_time = now
            return stats_r or self._cached_stats
        return self._cached_stats

    async def _init_setup(self, client):
        try:
            # 🛡️ Pre-flight Check: If IP is already banned, don't escalate.
            if client.is_backoff_active():
                wait_time = client.get_remaining_backoff()
                logger.warning(f"⛔ Startup Blocked: IP still banned. Waiting {wait_time}s...")
                return False

            # 0. Get ListenKey for WebSocket
            listen_key = await client.futures_stream_get_listen_key()
            self.listen_key = listen_key
            if listen_key: 
                logger.info(f"🔑 ListenKey created: {str(listen_key)[:5]}***")  # pyre-ignore
            else:
                logger.error("❌ Failed to create ListenKey. API might be limited.")
                return False

            # 1. Check Position Mode
            mode_data = await client.get_position_mode()
            if mode_data and mode_data.get('dualSidePosition') is True:
                logger.info("Switching to One-way Mode...")
                await client.change_position_mode(dualSidePosition=False)
            
            # 2. Check Account and Symbol Position
            acc = await self._get_cached_account(force=True)
            if not acc or 'positions' not in acc:
                logger.warning(f"⚠️ Could not fetch account info: {acc}. Bot will use WS fallback.")
                has_pos = False 
            else:
                pos = next((p for p in acc['positions'] if p['symbol'] == self.symbol), None)  # pyre-ignore
                has_pos = abs(safe_float(pos['positionAmt'])) > 0 if pos else False  # pyre-ignore
            
            # 3. Set Margin Type (Only if no position and API available)
            if not has_pos and acc:
                try: 
                    res = await client.change_margin_type(self.symbol, 'ISOLATED')
                    if res and res.get('code') == -4046: pass
                except Exception as e:
                    logger.debug(f"Margin Type change skipped/failed: {e}")
            
            # 4. Set Leverage
            await client.change_leverage(self.symbol, self.leverage)

            # 4.5 ตรวจ + เปิด BNB Fee Burn อัตโนมัติ (ลด fee 25%)
            try:
                bnb_status = await client.get_bnb_burn_status()
                if bnb_status and not bnb_status.get('spotBNBBurn', False):
                    await client.set_bnb_burn(spot_bnb_burn=True, interest_bnb_burn=True)
                    logger.info("🟡 BNB Fee Burn: เปิดอัตโนมัติแล้ว (ลด fee 25%)")
                else:
                    logger.info("🟡 BNB Fee Burn: เปิดอยู่แล้ว ✅")
            except Exception as e:
                logger.warning(f"🟡 BNB Fee Burn check skipped: {e}")

            # 5. Fetch Exchange Info for Precisions
            info = await client.get_exchange_info()
            if not info or 'symbols' not in info:
                logger.error("❌ Failed to fetch Exchange Info. Precisions will use defaults.")
                return False

            sym = next((s for s in info['symbols'] if s['symbol'] == self.symbol), None)
            if sym:
                qf = next((f for f in sym['filters'] if f['filterType']=='LOT_SIZE'), None)
                if qf: self.min_qty, self.step_size = float(qf['minQty']), float(qf['stepSize'])
                pf = next((f for f in sym['filters'] if f['filterType']=='PRICE_FILTER'), None)
                if pf: 
                    tick_size = float(pf['tickSize'])
                    self.p_prec = int(round(-math.log10(tick_size))) if tick_size > 0 else 1
                nf = next((f for f in sym['filters'] if f['filterType']=='MIN_NOTIONAL'), None)
                if nf: self.min_notional = float(nf.get('notional', nf.get('minNotional', 100.0)))
                self.q_prec = int(round(-math.log10(self.step_size))) if self.step_size > 0 else 3
                logger.info(f"✅ Setup Complete: {self.symbol} | P-Prec: {self.p_prec} | Q-Prec: {self.q_prec}")
                return True
            return False
        except Exception as e:
            logger.error(f"Init Error: {e}")
            return False

    def calculate_hypothetical_avg(self, current_qty, current_avg, next_buy_p, lot_usdt):
        if next_buy_p <= 0: return current_avg
        next_qty = lot_usdt / next_buy_p
        total_qty = abs(current_qty) + next_qty
        new_avg = ((abs(current_qty) * current_avg) + (next_qty * next_buy_p)) / total_qty
        return new_avg

    async def update_strategy_parameters(self, cur_p=None):
        """อัปเดตค่า Step และ TP ตามสภาวะตลาดและโหมดที่เลือก"""
        try:
            if not cur_p: cur_p = self.current_price
            
            # Fetch 24h stats with cache
            stats_r = await self._get_cached_stats()
            if not stats_r: return None
            
            stats = {"high": safe_float(stats_r['highPrice']), "low": safe_float(stats_r['lowPrice'])}
            vol_24h = (stats['high'] - stats['low']) / stats['low'] * 100 if stats['low'] > 0 else 5.0
            
            # Instantaneous Volatility (from buffer)
            # fallback: ถ้า buffer < 2 ใช้ vol_24h/24 เป็น proxy แทน inst_vol
            if len(self.price_buffer) >= 2:
                inst_vol = (max(self.price_buffer) - min(self.price_buffer)) / min(self.price_buffer) * 100
            else:
                inst_vol = vol_24h / 24  # proxy: 1-hour equivalent volatility

            base_step = max(0.5, min(2.0, vol_24h / 8))
            self.grid_step_pct = base_step + (inst_vol * 0.8)

            # --- APPLY STRATEGY MODIFIERS ---
            if self.strategy_mode == "SAFE":
                # Layer 10+ วิกฤต: ขยาย Grid Step 200% เพื่อรอ Mean-reversion ที่ไกลกว่า
                safe_mult = 3.0 if self.active_layers >= 10 else 1.5
                self.grid_step_pct *= safe_mult
                self.target_net_profit_pct = (0.10 + (self.grid_step_pct * 0.15)) * self.leverage
            elif self.strategy_mode == "PROFIT":
                self.target_net_profit_pct = (0.10 + (self.grid_step_pct * 0.10)) * self.leverage
            else: # NORMAL
                self.target_net_profit_pct = (0.18 + (self.grid_step_pct * 0.2)) * self.leverage

            # 📦 INVENTORY TP SCALING — Layer 8-9 เท่านั้นที่ลด TP เพื่อออกเร็วขึ้น
            # Layer 1-7: TP ปกติ (100%) — ไม่แตะ
            # Layer 8-9: TP ลดลง แต่ต้องคุ้มค่า fee สะสม (maker_fee × layers + maker_fee ปิด)
            # หลักการ: ออกเร็วกว่า แต่ต้องไม่ขาดทุนจาก fee
            if self.active_layers in (8, 9):
                # คำนวณ fee floor: fee ซื้อทุก layer + fee ปิด (ทั้งหมดเป็น maker)
                fee_floor_pct = (self.maker_fee * self.active_layers + self.maker_fee) * 100 * self.leverage
                # TP ขั้นต่ำ = fee_floor × 1.3 (กำไรสุทธิ 30% เหนือ fee)
                min_tp = fee_floor_pct * 1.3
                self.target_net_profit_pct = max(min_tp, self.target_net_profit_pct * 0.65)
            return stats
        except Exception as e:
            logger.error(f"Update Params Error: {e}")
            return None

    async def trading_engine(self, client):
        # 🟢 Wait for WebSocket + Account cache to be ready before first loop
        # ป้องกันบอทเปิดไม้ก่อน p_amt โหลดเสร็จ (เห็น p_amt=0 แล้วเปิดซ้ำ)
        await asyncio.sleep(3)
        # รอ account cache พร้อมก่อน (force fetch)
        for _attempt in range(10):
            acc_check = await self._get_cached_account(force=True)
            if acc_check and 'assets' in acc_check:
                logger.info("✅ Startup: Account cache ready — เริ่ม trading loop")
                break
            logger.info(f"⏳ Startup: รอ account data... ({_attempt+1}/10)")
            await asyncio.sleep(2)
        while True:
            try:
                # ⚡ Heartbeat Log + File (Watchdog อ่านจาก file แทน log parsing)
                logger.info("⚡ Trading Loop Tick")
                try:
                    with open("logs/heartbeat.txt", "w") as _hb:
                        _hb.write(str(time.time()))
                except Exception:
                    pass
                if self.gl_paused: await asyncio.sleep(5); continue

                # ใช้ราคาจาก WebSocket
                cur_p = self.current_price
                if cur_p <= 0:
                    ticker = await client.get_ticker(self.symbol)
                    if ticker:
                        cur_p = safe_float(ticker.get('price'))
                    
                    if cur_p <= 0: 
                        logger.debug("⏳ Price not available (REST Banned & WS Initializing). Waiting...")
                        await asyncio.sleep(5)
                        continue
                
                self.price_buffer.append(cur_p)
                if len(self.price_buffer) > 12: self.price_buffer.pop(0)
                
                stats: Any = await self.update_strategy_parameters(cur_p)
                if not stats: await asyncio.sleep(5); continue
                
                inst_vol = (max(self.price_buffer) - min(self.price_buffer)) / min(self.price_buffer) * 100

                # 📓 FLIP LOGGER — ตรวจ outcomes ที่ครบ 5 นาทีแล้ว
                self._process_flip_outcomes()

                # 🔴 KILL SWITCH CHECK
                if await self._check_kill_switch(inst_vol, client):
                    await asyncio.sleep(5); continue

                acc: Any = await self._get_cached_account()
                if not acc or 'assets' not in acc:
                    logger.warning("⚠️ Warning: Data account missing. Retrying in 10s...")
                    await asyncio.sleep(10); continue

                usdt = next((a for a in acc['assets'] if a['asset'] == 'USDT'), None)  # pyre-ignore
                w_bal, a_bal = (safe_float(usdt['walletBalance']), safe_float(usdt['availableBalance'])) if usdt else (0.0, 0.0)  # pyre-ignore
                # 🚀 UPGRADED LOT CALCULATION: Supporting recovery for $300-$500 balance
                base_lot_u = max(200.0, min((w_bal * 0.8 / 5) * self.leverage, 500.0))
                lot_scale = self._get_lot_scale()  # 📐 Dynamic Lot Sizing ตาม Layer
                lot_u = base_lot_u * lot_scale
                
                pos = next((p for p in acc['positions'] if p['symbol'] == self.symbol), None)  # pyre-ignore
                p_amt = safe_float(pos['positionAmt']) if pos else 0.0
                entry_p = safe_float(pos['entryPrice']) if pos else 0.0
                
                # 🌡️ VolatilityGate: ตรวจ σ/μ ratio ทุก loop
                vgate_ok = self._check_volatility_gate(cur_p)
                # 📅 Daily Loss: reset เมื่อขึ้นวันใหม่
                self._reset_daily_loss_if_new_day()

                if p_amt == 0:
                    self.active_layers, self.trailing_active, self.last_buy_price = 0, False, 0.0
                    self._position_open_time = 0.0  # Reset age timer

                    # 🧠 Post-TP Cooldown Guard: ถ้าเพิ่ง recover จาก deep hole (Layer ≥8)
                    # รอนานขึ้น (5 นาที) และต้องการ OBI แข็งแกร่งกว่าปกติก่อนเปิดรอบใหม่
                    post_tp_cooldown = 300.0 if getattr(self, '_last_closed_from_high_layer', False) else 60.0
                    obi_threshold = 0.1 if getattr(self, '_last_closed_from_high_layer', False) else -0.3

                    if (time.time() - self.last_close_time) > post_tp_cooldown:
                        self._last_closed_from_high_layer = False  # reset flag
                        p_range = (cur_p - stats['low']) / (stats['high'] - stats['low']) * 100 if stats['high'] > stats['low'] else 50  # pyre-ignore
                        # 📊 OBI+OFI Filter: ไม่เปิดไม้แรกถ้าแรงขายหนักมาก และ OFI ยืนยัน
                        # Deep OBI (Top 5 + distance-weighted) ยืนยันว่าแรงขายจริงไม่ใช่ Spoof จากระดับไกล
                        obi_ok = (self._obi_score >= obi_threshold or self._obi_last_update == 0) and self._obi_confirmed() and (self._obi_deep >= -0.35 or self._obi_last_update == 0)
                        if p_range < 85 and inst_vol < 0.4 and obi_ok and vgate_ok and not self._daily_kill_active:
                            if a_bal >= (lot_u / self.leverage): await self._execute_trade(client, "BUY", lot_u)
                else:
                    # ใช้ base_lot_u (ไม่ scale) เพื่อนับ layer จริง — ป้องกัน circular dependency
                    self.active_layers = max(1, int(round((abs(p_amt) * entry_p) / base_lot_u)))

                    # 🔄 Auto-recover strategy_mode จาก layer count (ป้องกัน reset เป็น NORMAL ตอน restart)
                    if self.last_buy_price <= 0 and self.strategy_mode == "NORMAL":
                        if self.active_layers >= self.MONITOR_CRITICAL_LAYERS:
                            self.strategy_mode = "SAFE"
                            logger.info(f"🔄 Mode recovered → SAFE (Layer {self.active_layers}/12)")
                        elif self.active_layers >= self.MONITOR_HIGH_LAYERS:
                            self.strategy_mode = "SAFE"
                            logger.info(f"🔄 Mode recovered → SAFE (Layer {self.active_layers}/12)")

                    # ⏳ บันทึกเวลาเปิด Position (สำหรับ Age Exit)
                    if self._position_open_time <= 0:
                        self._position_open_time = time.time()

                    # ⏳ VARIANCE AGE EXIT: บังคับปิดถ้าถือเกิน AGE_EXIT_HOURS
                    age_hours = (time.time() - self._position_open_time) / 3600
                    if self._position_open_time > 0 and age_hours >= self.AGE_EXIT_HOURS:
                        logger.warning(f"⏳ AGE EXIT: Position ถือมา {age_hours:.1f} ชั่วโมง (> {self.AGE_EXIT_HOURS}h) → บังคับปิดเพื่อ Variance Reset")
                        await self.tg.send_message(
                            f"⏳ *VARIANCE AGE EXIT*\n"
                            f"Position ถือมา `{age_hours:.1f}` ชั่วโมง (เกิน `{self.AGE_EXIT_HOURS}h`)\n"
                            f"บอทบังคับปิดเพื่อ Reset ความเสี่ยงครับ — PNL: `${safe_float(pos['unrealizedProfit']):.2f}`"
                        )
                        await self._execute_trade(client, "SELL", abs(p_amt), True)
                        self.last_close_time = time.time()
                        self.active_layers, self.trailing_active, self._position_open_time = 0, False, 0.0
                        self._monitor_last_alert = {"type": None, "pnl": 0.0, "layers": 0}
                        await asyncio.sleep(5)
                        continue

                    # 🚀 [HOTFIX] ฟื้นฟู Session: ใช้ entry_p แทน cur_p
                    if self.last_buy_price <= 0:
                        self.last_buy_price = entry_p if entry_p > 0 else cur_p
                        logger.info(f"🔄 Recovered Session: last_buy_price = ${self.last_buy_price:,.2f} (entry_p)")

                    # 🚫 OBI QUOTE CANCELLATION — ตรวจ pending GTX order ว่า OBI พลิกหรือยัง
                    # ถ้า OBI ต่ำกว่า threshold → cancel ทันที ป้องกัน Adverse Selection
                    if self._pending_gtx_order and not self._gtx_cancel_checked:
                        order_age = time.time() - self._pending_gtx_order["placed_at"]
                        obi_now = self._obi_score
                        # ตรวจหลังจากวาง 3 วินาที (ให้เวลา OB update) และก่อน 30 วินาที
                        if 3 <= order_age <= 30 and obi_now < self._gtx_cancel_obi_threshold:
                            try:
                                cancel_res = await client.cancel_all_open_orders(self.symbol)
                                logger.warning(
                                    f"🚫 OBI Quote Cancel: OBI={obi_now:+.2f} < {self._gtx_cancel_obi_threshold} "
                                    f"(order age {order_age:.0f}s) → ยกเลิก GTX order {self._pending_gtx_order['orderId']}"
                                )
                                await self.tg.send_message(
                                    f"🚫 *OBI Quote Cancel*\n"
                                    f"OBI พลิกเป็น `{obi_now:+.2f}` หลังวาง GTX\n"
                                    f"ยกเลิก order แล้ว — ป้องกัน Adverse Selection"
                                )
                                self._pending_gtx_order = None
                                self._gtx_cancel_checked = True
                            except Exception as ce:
                                logger.warning(f"OBI Cancel Error: {ce}")
                        elif order_age > 30:
                            # order น่าจะ fill แล้ว หรือ EXPIRED แล้ว → ล้าง pending
                            self._pending_gtx_order = None

                    # 🏛️ MARGIN RATIO MONITOR (Real-time Portfolio Monitoring)
                    total_maint_mm = safe_float(acc.get('totalMaintMargin', 0))
                    total_margin_bal = safe_float(acc.get('totalMarginBalance', 1))
                    m_ratio = (total_maint_mm / total_margin_bal) * 100 if total_margin_bal > 0 else 0.0

                    # 🚨 80/90% Alert Logic (Step-down monitoring)
                    if m_ratio >= 80:
                        alert_type = "DANGER 🚨" if m_ratio >= 90 else "WARNING ⚠️"
                        last_alert_time = getattr(self, "_margin_alert_last_time", 0)
                        # ส่งแจ้งเตือนทุก 15 นาที หรือเมื่อข้ามจาก Warning เป็น Danger
                        if time.time() - last_alert_time > 900 or (m_ratio >= 90 and getattr(self, "_margin_alert_level", 0) < 90):
                            await self.tg.send_message(
                                f"{alert_type} *MARGIN CALL ADVISORY*\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"🏦 พอร์ตของคุณเข้าเขตเสี่ยงสูงครับ!\n"
                                f"📊 Margin Ratio: `{m_ratio:.2f}%`\n"
                                f"💰 Wallet Balance: `${w_bal:,.2f}`\n"
                                f"🛡️ *แนะนำ:* พิจารณาเติมมาร์จิ้นส่วนกลาง หรือปิดบางเหรียญเพื่อลด Risk ครับ"
                            )
                            self._margin_alert_last_time = time.time()
                            self._margin_alert_level = 90 if m_ratio >= 90 else 80

                    logger.info(f"📊 Calc: Amt {p_amt} * Entry {entry_p} / Lot {lot_u:.0f} (scale {lot_scale:.0%}) = Layers {self.active_layers} (M.Ratio: {m_ratio:.1f}%)")
                    layer_mult = 1.0 + (max(0, self.active_layers - 3) * 0.25)
                    final_step = self.grid_step_pct * layer_mult
                    
                    target_ref_p = self.last_buy_price
                    self.next_buy_price = target_ref_p * (1 - (final_step/100))
                    self.predicted_avg_price = self.calculate_hypothetical_avg(p_amt, entry_p, self.next_buy_price, lot_u)
                    
                    # 🔮 DCA CONDITION: Allow up to 12 layers for recovery
                    # 📊 OBI+OFI Filter: รอถ้าแรงขายหนักและ OFI ยืนยัน ยกเว้นไม้สุดท้าย
                    # ⚖️ INVENTORY SKEW CHECK
                    margin_used = (abs(p_amt) * entry_p) / self.leverage
                    inv_status = self._check_inventory_skew(p_amt, entry_p, cur_p, margin_used)
                    if inv_status == 'toxic' and (time.time() - self._last_report_time) > 300:
                        await self.tg.send_message(f"⚠️ *Inventory Toxic!* ขาดทุนเกิน `{self.INV_MAX_LOSS_PCT}%` ของ margin\nพิจารณาปิด Position ด้วยตนเองครับ")
                        self._last_report_time = time.time()


                    # 🔔 PRICE LEVEL ALERT — ตรวจแนวรับสำคัญ
                    if self.PRICE_ALERT_LEVELS:
                        now_pa = time.time()
                        for (pa_label, pa_price, pa_dir) in self.PRICE_ALERT_LEVELS:
                            triggered = (pa_dir == "below" and cur_p < pa_price) or \
                                        (pa_dir == "above" and cur_p > pa_price)
                            last_pa_time = self._price_alert_last_time.get(pa_label, 0)
                            if triggered and (now_pa - last_pa_time) > self._price_alert_cooldown:
                                self._price_alert_last_time[pa_label] = now_pa
                                dist_pct = abs(cur_p - pa_price) / pa_price * 100
                                await self.tg.send_message(
                                    f"🔴 *PRICE ALERT — หลุดแนวรับ!*\n"
                                    f"ระดับ: `{pa_label}`\n"
                                    f"เป้า: `${pa_price:,.1f}`\n"
                                    f"ปัจจุบัน: `${cur_p:,.1f}` ({dist_pct:.2f}% ใต้แนวรับ)\n"
                                    f"{'🚨 *ใกล้ Liquidation Price มาก!*' if 'LIQ' in pa_label else ''}"
                                )
                                logger.warning(f"🔔 PRICE ALERT: {pa_label} @ ${pa_price:,.1f} | cur=${cur_p:,.1f}")

                    # 🛡️ EQUITY KILL SWITCH: ตรวจ Drawdown และ Emergency Balance ก่อน DCA
                    pnl_usdt_check = safe_float(pos['unrealizedProfit']) if pos else 0.0
                    # FIXED: ใช้ Wallet Balance (w_bal) เป็นตัวหารเพื่อความแม่นยำ
                    equity_drawdown_pct = pnl_usdt_check / max(w_bal, 1.0)
                    
                    # [NEW] Auto-Reset Equity Kill if recovered (Drawdown < -15%)
                    if self._equity_kill_active and equity_drawdown_pct > -0.15:
                        self._equity_kill_active = False
                        logger.info(f"🛡️ EQUITY KILL AUTO-RESET: Drawdown recovered to {equity_drawdown_pct*100:.1f}%")
                        await self.tg.send_message("🛡️ *EQUITY KILL AUTO-RESET:*\nตลาดฟื้นตัวจน Drawdown ต่ำกว่า `-15%` แล้ว\nบอทกลับมาทำงาน (DCA) ตามปกติครับ")
                    if not self._equity_kill_active and equity_drawdown_pct <= self.EQUITY_DRAWDOWN_LIMIT:
                        self._equity_kill_active = True
                        logger.warning(f"🛡️ EQUITY KILL: Drawdown {equity_drawdown_pct*100:.1f}% เกิน {self.EQUITY_DRAWDOWN_LIMIT*100:.0f}% → หยุด DCA + ยกเลิก Orders ทั้งหมด")
                        try:
                            await client.cancel_all_open_orders(self.symbol)
                            logger.info("🛡️ Cancel All Open Orders สำเร็จ (Equity Kill)")
                        except Exception as ce:
                            logger.warning(f"🛡️ Cancel Orders failed: {ce}")
                        await self.tg.send_message(
                            f"🛡️ *EQUITY KILL SWITCH ACTIVE*\n"
                            f"Drawdown: `{equity_drawdown_pct*100:.1f}%` เกิน `{self.EQUITY_DRAWDOWN_LIMIT*100:.0f}%`\n"
                            f"PNL: `${pnl_usdt_check:.2f}` | Balance: `${a_bal:.2f}`\n"
                            f"🚫 ยกเลิก Open Orders ทั้งหมดแล้ว\n"
                            f"⚠️ บอทหยุด DCA จนกว่าจะสั่ง `🔄 NORMAL` หรือ `▶️ รันต่อ` ครับ"
                        )

                    # Deep OBI gate เพิ่มความแม่นยำ แต่ bypass เมื่อ layer สูง (≥10) เพราะต้อง DCA ต่อ
                    obi_dca_ok = (self._obi_score >= -0.3 and self._obi_confirmed() and self._obi_deep >= -0.35) or self.active_layers >= 10 or self._obi_last_update == 0
                    if cur_p <= self.next_buy_price and self.active_layers < 12 and obi_dca_ok:
                        logger.info(f"🔮 Prediction: Buy @ {self.next_buy_price:.1f} -> New Avg {self.predicted_avg_price:.1f} | OBI {self._obi_score:+.2f} | Lot Scale {lot_scale:.0%}")
                        # 🛡️ Block DCA ตามลำดับความสำคัญ
                        if self._daily_kill_active:
                            logger.warning(f"📅 DCA BLOCKED (Daily Kill): ขาดทุนสะสม ${self._daily_loss_total:.2f} — หยุดทั้งวัน")
                        elif self._check_order_throttle():
                            pass  # log อยู่ใน _check_order_throttle แล้ว
                        elif self._equity_kill_active:
                            logger.warning(f"🛡️ DCA BLOCKED (Equity Kill Active): Layer {self.active_layers}, Drawdown {equity_drawdown_pct*100:.1f}%")
                        elif not vgate_ok:
                            logger.warning(f"🌡️ DCA BLOCKED (VolatilityGate): σ/μ ต่ำ → Trending detected, รอ Re-arm {self.VGATE_REARM_BARS} bars")
                        elif a_bal < self.EMERGENCY_BAL_LIMIT:
                            logger.warning(f"🛡️ DCA BLOCKED (Emergency Balance ${a_bal:.2f} < ${self.EMERGENCY_BAL_LIMIT}): รักษา Margin ไว้ก่อน")
                            if (time.time() - self._last_report_time) > 300:
                                await self.tg.send_message(f"🛡️ *EMERGENCY BALANCE LOCK*\nAvailable: `${a_bal:.2f}` (ต่ำกว่า `${self.EMERGENCY_BAL_LIMIT}`)\nบอทหยุด DCA เพื่อรักษา Margin ครับ")
                                self._last_report_time = time.time()
                        elif a_bal >= (lot_u / self.leverage):
                            await self._execute_trade(client, "BUY", lot_u)
                        else:
                            logger.warning(f"⚠️ Insufficient Balance to DCA: Need ${(lot_u / self.leverage):.2f} USDT, but have ${a_bal:.2f} USDT Available.")
                            if (time.time() - self._last_report_time) > 60:
                                await self.tg.send_message(f"🚨 *ยอดเงินไม่พอเปิดไม้แก้!*\nต้องการ: `${(lot_u / self.leverage):.2f}` USDT\nมีอยู่: `${a_bal:.2f}` USDT\n(กรุณาเติมเงินเข้า Futures Wallet หรือโอนเงินจาก Spot มาครับ)")
                                self._last_report_time = time.time()
                    
                    pnl_pct = (((cur_p - entry_p) / entry_p * 100) * self.leverage)
                    # 💰 Fee-aware Break-Even: รวม fee ทุก layer ที่ซื้อมา (taker) + fee ตอนปิด (taker)
                    # total_fee_pct = (layers_ซื้อ × taker_fee) + taker_fee_ปิด
                    total_buy_fee = self.taker_fee * self.active_layers  # fee ซื้อสะสม
                    close_fee = self.taker_fee                            # fee ปิด 1 ครั้ง
                    be_p = entry_p * (1 + total_buy_fee + close_fee)
                    pnl_usdt = safe_float(pos['unrealizedProfit']) if pos else 0.0

                    # 🤖 AUTO-MONITOR: ตรวจสอบและตัดสินใจอัตโนมัติ
                    await self._auto_monitor(client, p_amt, pnl_usdt, entry_p)

                    # 🔒 PROFIT LOCK TRAILING
                    # Layer 1-5: activate เมื่อถึง target_net_profit_pct (เดิม)
                    # Layer 6+:  activate เมื่อ PNL > $2 (ปกป้องกำไรที่ recover มาจาก deep hole)
                    profit_lock_trigger = pnl_usdt > 2.0 and self.active_layers >= 6
                    if pnl_pct >= self.target_net_profit_pct or profit_lock_trigger:
                        if not self.trailing_active:
                            self.trailing_active, self.peak_price = True, cur_p
                            if profit_lock_trigger and pnl_pct < self.target_net_profit_pct:
                                logger.info(f"🔒 Profit Lock Trailing activated: PNL ${pnl_usdt:.2f} (Layer {self.active_layers})")
                            self._trailing_state_save()
                        if cur_p > self.peak_price:
                            self.peak_price = cur_p
                            self._trailing_state_save()

                    # 📊 Trailing distance — Layer สูง ใช้ distance กว้างขึ้น (ให้ราคาหายใจ)
                    # Layer 1-5: 0.05% | Layer 6-9: 0.15% | Layer 10+: 0.25%
                    if self.active_layers >= 10:
                        trail_dist = 0.0025
                    elif self.active_layers >= 6:
                        trail_dist = 0.0015
                    else:
                        trail_dist = 0.0005

                    trailing_trigger = self.peak_price * (1 - trail_dist)

                    # 📊 OBI Boost: ถ้า trailing active และ OBI < -0.6 (แรงขายหนักมาก) → ปิดเร็วขึ้น
                    if self.trailing_active and self._obi_score <= -0.6:
                        trailing_trigger = self.peak_price * (1 - trail_dist / 2)  # ปิดเร็วขึ้นเมื่อวาฬขาย

                    if self.trailing_active and cur_p <= trailing_trigger:
                        if cur_p > be_p:
                            await self.tg.send_message(
                                f"🔒 *PROFIT LOCK*: ราคาร่วงจาก Peak `${self.peak_price:,.0f}` → `${cur_p:,.0f}` ({trail_dist*100:.2f}%)\n"
                                f"💰 PNL: `${pnl_usdt:.2f}` → ปิด Position เพื่อรักษากำไร!"
                            )
                            await self._execute_trade(client, "SELL", abs(p_amt), True)
                            self.last_close_time = time.time()
                            self.active_layers, self.trailing_active = 0, False
                            self._monitor_last_alert = {"type": None, "pnl": 0.0, "layers": 0}
                        else:
                            self.trailing_active = False
                            self._trailing_state_save()
                
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Engine Error: {e}")
                await asyncio.sleep(10)

    async def _execute_trade(self, client, side, amt, is_close=False):
        try:
            # 🛡️ SAE Price Safety Check
            cur_p = self.current_price
            if cur_p <= 0:
                ticker_r = await client.get_ticker(self.symbol)
                cur_p = safe_float(ticker_r.get('price')) if ticker_r else 0.0
            
            if cur_p <= 0:
                logger.error("❌ Trade Failed: Price not available (Banned or WS Delay)")
                return

            if is_close:
                params = {'order_type': 'MARKET', 'quantity': f"{amt:.{self.q_prec}f}", 'reduceOnly': True}
            else:
                raw_q = amt / cur_p
                qty = max(self.min_qty, math.floor(raw_q / self.step_size) * self.step_size)
                limit_p = cur_p * 0.9999 if side == "BUY" else cur_p * 1.0001
                params = {'order_type': 'LIMIT', 'timeInForce': 'GTX', 'quantity': f"{qty:.{self.q_prec}f}", 'price': f"{limit_p:.{self.p_prec}f}"}
            
            res = await client.create_order(self.symbol, side, **params)
            order_status = res.get('status') if res else None
            # GTX (Post-Only) จะ return status=EXPIRED ถ้า reject — ไม่นับว่าสำเร็จ
            order_filled = res and res.get('orderId') and order_status not in ('EXPIRED', 'CANCELED', 'REJECTED', None)
            if order_filled:
                # ⚡ บันทึก timestamp สำหรับ Rapid-Fire Throttle
                self._order_timestamps.append(time.time())
                if is_close:
                    self.last_close_time = time.time()
                    self._pending_gtx_order = None  # ปิดแล้ว ล้าง pending
                    # 📅 บันทึก Realized PnL สำหรับ Daily Loss Tracker
                    try:
                        acc_snap = await self._get_cached_account(force=True)
                        pos_snap = next((p for p in acc_snap.get('positions', []) if p['symbol'] == self.symbol), None) if acc_snap else None
                        realized = safe_float(pos_snap.get('realizedProfit', 0)) if pos_snap else 0.0
                        if realized != 0.0:
                            self._record_realized_pnl(realized)
                    except Exception:
                        pass
                else:
                    self.last_buy_price = cur_p
                    # 💰 MAKER REBATE TRACKING: บันทึกค่า fee ที่ประหยัดได้
                    # GTX (Maker) fee = maker_fee | baseline ถ้าใช้ Market = taker_fee
                    # notional = qty * cur_p
                    if not is_close:
                        raw_q = amt / cur_p
                        notional = math.floor(raw_q / self.step_size) * self.step_size * cur_p
                        rebate = (self.taker_fee - self.maker_fee) * notional
                        self._rebate_saved_total += rebate
                        self._rebate_trade_count += 1
                        logger.info(f"💰 Maker Rebate: +${rebate:.4f} | สะสม ${self._rebate_saved_total:.4f} ({self._rebate_trade_count} trades)")
                    # 🚫 เก็บ pending GTX order สำหรับ OBI Cancel check
                    if res and res.get('orderId') and not is_close:
                        self._pending_gtx_order = {
                            "orderId": str(res['orderId']),
                            "price": cur_p,
                            "placed_at": time.time()
                        }
                        self._gtx_cancel_checked = False
                await self.tg.send_message(f"{'🔴 ปิด' if is_close else '🔵 เปิด (GTX)'} สำเร็จ!")
                # Force refresh account after trade
                await self._get_cached_account(force=True)
            elif res and res.get('orderId') and order_status == 'EXPIRED':
                logger.warning(f"GTX Order EXPIRED (Post-Only Rejected): {res.get('orderId')} — ไม่อัปเดต last_buy_price")
                self._pending_gtx_order = None
            else:
                logger.error(f"Order Failed: {res}")
        except Exception as e:
            logger.error(f"Trade Execution Error: {e}")

    async def send_combined_report(self):
        try:
            # 🛡️ Cooldown Check (30s) to avoid double reporting spam
            now = time.time()
            if (now - self._last_report_time) < 30:
                left = int(30 - (now - self._last_report_time))
                await self.tg.send_message(f"⏳ *ระบบกำลังดึงข้อมูล...*\n(กรุณารออีก {left} วินาทีเพื่อเช็คพอร์ตใหม่ ป้องกัน API แบนครับ)")
                return
            self._last_report_time = now

            acc = await self._get_cached_account()
            if not acc or 'assets' not in acc:
                await self.tg.send_message("⚠️ *Notice:* ระบบกำลังประหยัดทรัพยากร (API Weight)")
                return

            mark_data = await self.client_gl.get_mark_price(self.symbol)
            mark_p = safe_float(mark_data.get('markPrice')) if mark_data else self.current_price
            
            usdt = next((a for a in acc['assets'] if a['asset'] == 'USDT'), None)  # pyre-ignore
            w_bal, a_bal = (safe_float(usdt['walletBalance']), safe_float(usdt['availableBalance'])) if usdt else (0.0, 0.0)  # pyre-ignore
            pos = next((p for p in acc['positions'] if p['symbol'] == self.symbol), None)  # pyre-ignore
            p_amt = safe_float(pos['positionAmt']) if pos else 0.0
            
            m_icon = "🛡️ SAFE" if self.strategy_mode == "SAFE" else ("💸 PROFIT" if self.strategy_mode == "PROFIT" else "🔄 NORMAL")
            status_text = "🟢 ONLINE" if not self.gl_paused else "🔴 PAUSED"

            # 🖼️ PREMIUM DASHBOARD CONSTRUCTION
            msg =  f"🏰 *COMMANDER DASHBOARD v2.0*\n"
            msg += f"━━━━━━━━━━━━━━━━━━\n"
            msg += f"📡 Status: `{status_text}` | Mode: `{m_icon}`\n"
            msg += f"💰 Balance: `${w_bal:,.2f}` (`${a_bal:,.2f}` avail)\n"
            msg += f"⚙️ Grid Step: `{self.grid_step_pct:.3f}%` | TP: `+{self.target_net_profit_pct:.2f}%` (Net)\n"
            msg += f"━━━━━━━━━━━━━━━━━━\n"
            
            if p_amt != 0:
                entry_p = safe_float(pos['entryPrice']) if pos else 0.0
                
                # 🔄 RE-CALCULATE LAYERS ON THE FLY (FOR ACCURACY)
                c_lot_u = max(200.0, min((w_bal * 0.8 / 5) * self.leverage, 500.0))
                self.active_layers = max(1, int(round((abs(p_amt) * entry_p) / c_lot_u)))
                
                pnl = safe_float(pos['unrealizedProfit']) if pos else 0.0
                pnl_p = (pnl / (abs(p_amt) * entry_p / self.leverage)) * 100 if entry_p > 0 else 0.0
                layers_now = max(1, int(round((abs(p_amt) * entry_p) / c_lot_u))) if c_lot_u > 0 else 1
                be_p = entry_p * (1 + self.taker_fee * layers_now + self.taker_fee)

                side_icon = "📈 LONG" if p_amt > 0 else "📉 SHORT"
                pnl_icon = "🔥" if pnl >= 0 else "❄️"
                
                msg += f"🚀 *POSITION: {side_icon} | {abs(p_amt):.3f} BTC*\n"
                msg += f"├ {pnl_icon} PNL: *${pnl:,.2f}* (`{pnl_p:+.2f}%`)\n"
                msg += f"├ Entry: `${entry_p:,.2f}`\n"
                msg += f"├ Mark:  `${mark_p:,.2f}`\n"
                msg += f"└ *Net BE:* `${be_p:,.2f}`\n"
                
                if self.next_buy_price > 0:
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    msg += f"🔮 *ACTION PLAN: (DCA Strategy)*\n"
                    layer_text = f"🚨 MAX reached" if self.active_layers >= 12 else f"Layer: `{self.active_layers}/12`"
                    msg += f"├ {layer_text}\n"
                    msg += f"├ Buy Next: `${self.next_buy_price:,.1f}`\n"
                    msg += f"└ Predicted Avg: `${self.predicted_avg_price:,.1f}`\n"

                # 🧮 MARGIN ANALYSIS — แสดงเฉพาะเมื่อ Liq. ใกล้ (< 6%) เพื่อไม่รกหน้าจอ
                if pos and a_bal >= 1.0 and abs(p_amt) > 0:
                    liq_p = safe_float(pos.get('liquidationPrice', 0))
                    iso_margin = safe_float(pos.get('isolatedWallet', 0))
                    if liq_p > 0 and iso_margin > 0 and mark_p > 0 and (mark_p - liq_p) / mark_p * 100 < 6.0:
                        cur_dist = (mark_p - liq_p) / mark_p * 100

                        # Option A: เติม margin เพียวๆ
                        new_margin_a = iso_margin + a_bal
                        new_liq_a = entry_p - (new_margin_a / abs(p_amt))
                        dist_a = (mark_p - new_liq_a) / mark_p * 100

                        # Option DCA: เปิดไม้ถัดไป
                        if self.next_buy_price > 0 and self.active_layers < 12:
                            lot_u_est = max(200.0, min((w_bal * 0.8 / 5) * self.leverage, 500.0))
                            dca_qty = (a_bal * self.leverage) / self.next_buy_price
                            new_qty = abs(p_amt) + dca_qty
                            new_entry = (abs(p_amt) * entry_p + dca_qty * self.next_buy_price) / new_qty
                            new_margin_dca = iso_margin + a_bal
                            new_liq_dca = new_entry - (new_margin_dca / new_qty)
                            dist_dca = (mark_p - new_liq_dca) / mark_p * 100
                            safe_a   = "✅" if dist_a   >= 5.0 else "⚠️"
                            safe_dca = "✅" if dist_dca >= 5.0 else "⚠️"
                            msg += f"━━━━━━━━━━━━━━━━━━\n"
                            msg += f"🧮 *MARGIN ANALYSIS* (ใช้ `${a_bal:.2f}` avail)\n"
                            msg += f"├ ตอนนี้:  Liq. `${liq_p:,.0f}` ห่าง `{cur_dist:.2f}%`\n"
                            msg += f"├ {safe_a} เติม Margin:  Liq. `${new_liq_a:,.0f}` ห่าง `{dist_a:.2f}%`\n"
                            msg += f"└ {safe_dca} DCA ไม้ {self.active_layers+1}: Liq. `${new_liq_dca:,.0f}` ห่าง `{dist_dca:.2f}%`\n"
                        else:
                            safe_a = "✅" if dist_a >= 5.0 else "⚠️"
                            msg += f"━━━━━━━━━━━━━━━━━━\n"
                            msg += f"🧮 *MARGIN ANALYSIS* (ใช้ `${a_bal:.2f}` avail)\n"
                            msg += f"├ ตอนนี้:       Liq. `${liq_p:,.0f}` ห่าง `{cur_dist:.2f}%`\n"
                            msg += f"└ {safe_a} เติม Margin: Liq. `${new_liq_a:,.0f}` ห่าง `{dist_a:.2f}%`\n"
            else:
                msg += f"💤 *STATUS:* พอร์ตว่าง (กำลังรอสัญญาณเข้าที่ดีที่สุด)\n"
                msg += f"📍 Last Price: `${self.current_price:,.2f}`\n"
            
            msg += f"━━━━━━━━━━━━━━━━━━\n"
            regime, rng = self._get_regime()
            regime_icon = {"CHOPPY": "↔️", "VOLATILE": "⚡", "TRENDING": "📈", "WARMING": "🌡️", "UNKNOWN": "❓", "ERROR": "⚠️"}.get(regime, "❓")
            msg += f"🧭 *Regime:* `{regime_icon} {regime}` (`{rng:.4f}%` range) | Trade OBI: `{self._trade_obi:+.2f}`\n"
            msg += f"━━━━━━━━━━━━━━━━━━\n"
            # 💰 MAKER REBATE SUMMARY
            session_hrs = (time.time() - self._rebate_session_start) / 3600
            msg += f"💰 *Maker Rebate Saved:* `${self._rebate_saved_total:.4f}` ({self._rebate_trade_count} trades, {session_hrs:.1f}h)\n"
            msg += f"━━━━━━━━━━━━━━━━━━\n"
            msg += f"🐳 *Whale Signal:* {self._get_whale_signal()}"
            
            await self.tg.send_message(msg)
        except Exception as e:
            logger.error(f"Combined Report Error: {e}")
            await self.tg.send_message("❌ เกิดข้อผิดพลาดในการสร้างรายงาน")

    async def emergency_close(self):
        """ปิด Position ทั้งหมดทันที"""
        try:
            acc = await self.client_gl.get_account()
            if not acc or 'positions' not in acc:
                await self.tg.send_message("⚠️ *SAE Notice:* ไม่สามารถปิดออเดอร์ได้ในขณะนี้เนื่องจาก Weight เต็ม")
                return

            pos = next((p for p in acc['positions'] if p['symbol'] == self.symbol), None)
            p_amt = safe_float(pos['positionAmt']) if pos else 0.0
            if abs(p_amt) > 0:
                side = "SELL" if p_amt > 0 else "BUY"
                await self._execute_trade(self.client_gl, side, abs(p_amt), True)
                await self.tg.send_message("💥 *Emergency Close:* สั่งปิด Position ทั้งหมดเรียบร้อยครับ! (บอทจะพักการเข้า 1 นาที)")
            else:
                await self.tg.send_message("⚠️ ไม่มี Position ค้างอยู่ครับ")
        except Exception as e:
            logger.error(f"Emergency Close Error: {e}")
            await self.tg.send_message("❌ เกิดข้อผิดพลาดในการปิด Position")

    async def send_trade_report(self):
        try:
            end_t = int(time.time() * 1000)
            history = await self.client_gl.get_income_history(self.symbol, startTime=(end_t-(24*3600*1000)), endTime=end_t)
            
            if history is None:
                await self.tg.send_message("⚠️ *Notice:* ไม่สามารถดึงประวัติกำไรได้ชั่วคราว (API Weight)")
                return

            pnl, fee, count = 0.0, 0.0, 0
            for i in history:
                if i['incomeType'] == 'REALIZED_PNL': pnl += safe_float(i['income'])
                elif i['incomeType'] == 'COMMISSION': fee += abs(safe_float(i['income']))
                if i['incomeType'] == 'REALIZED_PNL': count += 1
            
            net_pnl = pnl - fee
            pnl_icon = "🟢" if net_pnl >= 0 else "🔴"
            
            msg =  f"💰 *รายงานกำไร (รอบ 24 ชม.)*\n"
            msg += f"━━━━━━━━━━━━━━━━━━\n"
            msg += f"📈 Gross PNL: `+{pnl:,.4f} USDT`\n"
            msg += f"⛽ Total Fees: `-{fee:,.4f} USDT`\n"
            msg += f"━━━━━━━━━━━━━━━━━━\n"
            msg += f"{pnl_icon} *NET PROFIT: `${net_pnl:,.4f} USDT`*\n"
            msg += f"📊 จำนวนออเดอร์: `{count}` รอบ\n"
            msg += f"━━━━━━━━━━━━━━━━━━\n"
            msg += f"✨ *Compound Status:* ระบบกำลังพิจารณานำกำไรไปทบทุนในไม้ถัดไป..."
            
            await self.tg.send_message(msg)
        except Exception as e:
            logger.error(f"Trade Report Error: {e}")
            await self.tg.send_message("❌ ไม่สามารถดึงรายงานกำไรได้ในขณะนี้")

    async def send_exit_analysis(self, is_auto=False):
        """🧮 วิเคราะห์ทางออก: คำนวณทางเลือก Cut Loss / รอ / DCA"""
        try:
            # ใช้ Cache Account
            acc = await self._get_cached_account()
            if not acc or 'positions' not in acc:
                if not is_auto: await self.tg.send_message("⚠️ ไม่สามารถดึงข้อมูลพอร์ตได้ในขณะนี้")
                return

            pos = next((p for p in acc['positions'] if p['symbol'] == self.symbol), None)
            p_amt_raw = safe_float(pos['positionAmt']) if pos else 0.0
            if p_amt_raw == 0:
                if not is_auto: await self.tg.send_message("💤 *ไม่มี Position ค้างอยู่ครับ*")
                return

            p_amt = abs(p_amt_raw)
            side = "LONG" if p_amt_raw > 0 else "SHORT"
            entry_p = safe_float(pos['entryPrice'])
            cur_p = self.current_price
            if cur_p <= 0:
                ticker = await self.client_gl.get_ticker(self.symbol)
                cur_p = safe_float(ticker.get('price')) if ticker else 0.0

            pnl = safe_float(pos['unrealizedProfit'])
            usdt = next((a for a in acc['assets'] if a['asset'] == 'USDT'), None)
            w_bal = safe_float(usdt['walletBalance']) if usdt else 1.0
            a_bal = safe_float(usdt['availableBalance']) if usdt else 0.0
            
            # คำนวณ % ขาดทุนเทียบกับ Margin
            margin_used = (p_amt * entry_p) / self.leverage
            pnl_p = (pnl / margin_used) * 100 if margin_used > 0 else 0.0

            # 🛠️ 1. Scenario A: ปิดทันที (Cut Loss)
            remaining_bal = w_bal + pnl

            # 🛠️ 2. Scenario B: รอราคากลับ (Break-Even) — รวม fee ทุก layer
            cur_layers = max(1, int(round((p_amt * entry_p) / (self.leverage * 500)))) if entry_p > 0 else 1
            total_fee = self.taker_fee * cur_layers + self.taker_fee  # fee ซื้อสะสม + fee ปิด
            if side == "LONG":
                be_p = entry_p * (1 + total_fee)
                needed_pct = (be_p - cur_p) / cur_p * 100 if cur_p > 0 else 0.0
            else:
                be_p = entry_p * (1 - total_fee)
                needed_pct = (cur_p - be_p) / cur_p * 100 if cur_p > 0 else 0.0

            # 🛠️ 3. Scenario C: DCA เพิ่ม (ใช้ Avail ทั้งหมด)
            if a_bal > 1.0:
                dca_lot_u = a_bal * self.leverage
                dca_qty = dca_lot_u / cur_p
                new_qty = p_amt + dca_qty
                new_layers = cur_layers + 1
                new_total_fee = self.taker_fee * new_layers + self.taker_fee

                if side == "LONG":
                    new_entry = (p_amt * entry_p + dca_lot_u) / new_qty
                    new_be_p = new_entry * (1 + new_total_fee)
                    new_needed_pct = (new_be_p - cur_p) / cur_p * 100 if cur_p > 0 else 0.0
                else:
                    new_entry = (p_amt * entry_p + dca_qty * cur_p) / new_qty
                    new_be_p = new_entry * (1 - new_total_fee)
                    new_needed_pct = (cur_p - new_be_p) / cur_p * 100 if cur_p > 0 else 0.0
            else:
                new_entry, new_be_p, new_needed_pct = 0, 0, 0

            msg = "🧮 *วิเคราะห์ทางออก (EXIT ANALYSIS)*\n"
            if is_auto:
                msg = "🔔 *AUTO-ADVISOR: คำแนะนำรายชั่วโมง*\n"
            msg += "━━━━━━━━━━━━━━━━━━\n"
            msg += f"🚀 Position: `{side} {p_amt:.3f} BTC`\n"
            msg += f"Entry: `${entry_p:,.2f}`\n"
            msg += f"PNL: `${pnl:,.2f}` (`{pnl_p:+.2f}%`)\n"
            msg += "━━━━━━━━━━━━━━━━━━\n"
            
            # Scenario A
            msg += f"🔴 *Scenario A: ปิดทันที*\n"
            msg += f"└ ขาดทุน: `${abs(pnl):,.2f}` | เหลือทุน: `${remaining_bal:,.2f}`\n\n"
            
            # Scenario B
            msg += f"🟡 *Scenario B: ถือรอ (No DCA)*\n"
            msg += f"└ ต้อง{'ขึ้น' if side == 'LONG' else 'ลง'}อีก: `+{needed_pct:.2f}%` (ถึง `${be_p:,.2f}`)\n\n"
            
            # Scenario C
            if a_bal > 1.0:
                msg += f"🟢 *Scenario C: DCA สู้ (ใช้เงิน `${a_bal:.1f}`)*\n"
                msg += f"└ Entry ใหม่: `${new_entry:,.2f}`\n"
                msg += f"└ ต้อง{'ขึ้น' if side == 'LONG' else 'ลง'}อีก: `+{new_needed_pct:.2f}%` (ถึง `${new_be_p:,.2f}`)\n"
            else:
                msg += f"⚪ *Scenario C: DCA (เงินไม่พอ)*\n"
                msg += f"└ กรุณาเติมเงินเพื่อลด Entry เฉลี่ยครับ\n"
            
            msg += "━━━━━━━━━━━━━━━━━━\n"
            
            # 💡 Recommendation
            rec = "⏳ *แนะนำ:* ตลาดผันผวนสูง แนะนำ 'ถือรอ' และเฝ้า Liq. Price"
            if pnl_p <= -50:
                rec = "🚨 *แนะนำ:* ขาดทุนหนัก (Toxic) พิจารณา 'Cut Loss' เพื่อรักษาทุนที่เหลือ"
            elif needed_pct <= 0.6:
                rec = "✅ *แนะนำ:* ใกล้คุ้มทุนมากแล้ว แนะนำ 'รอ' หรือตั้ง TP/BE ไว้ครับ"
            elif a_bal >= 30 and (needed_pct - new_needed_pct) > 0.5:
                rec = "💪 *แนะนำ:* หากมีเงินสำรอง การ 'DCA' จะช่วยให้หลุดได้ไวขึ้นมาก"
            
            msg += f"💡 {rec}\n\n"
            
            # 🏛️ 4. TOP-UP ADVISOR & MARGIN MONITOR (Smart Cross/Iso Support)
            liq_p = safe_float(pos.get('liquidationPrice', 0))
            margin_type = pos.get('marginType', 'isolated').lower()
            maint_margin = safe_float(pos.get('maintMargin', 0))
            
            # ดึงข้อมูลภาพรวมพอร์ต (สำหรับ Cross Margin Monitor)
            total_maint_margin = safe_float(acc.get('totalMaintMargin', 0))
            total_margin_balance = safe_float(acc.get('totalMarginBalance', 1))
            margin_ratio = (total_maint_margin / total_margin_balance) * 100 if total_margin_balance > 0 else 0.0

            msg += "💰 *วิเคราะห์หลักประกัน (MARGIN INSIGHT)*\n"
            msg += "━━━━━━━━━━━━━━━━━━\n"
            msg += f"🏦 โหมด: `{margin_type.upper()}`\n"
            msg += f"📊 Margin Ratio: `{margin_ratio:.2f}%` "
            
            # บันไดการแจ้งเตือน (Margin Ratio Warning)
            if margin_ratio >= 90:
                msg += "🚨 *CRITICAL*"
            elif margin_ratio >= 80:
                msg += "⚠️ *WARNING*"
            else:
                msg += "✅ *SAFE*"
            msg += "\n"

            if liq_p > 0 and abs(p_amt) > 0:
                # --- ส่วนที่ 1: เติมเพื่อความปลอดภัย (Safety Buffer 10-15% สำหรับ Cross) ---
                # ปรับ Buffer ตามคำแนะนำ (Cross ใช้ 15%, Iso ใช้ 5%)
                buffer_rate = 0.15 if margin_type == "cross" else 0.05
                target_liq_safe = cur_p * ((1 - buffer_rate) if side == "LONG" else (1 + buffer_rate))
                
                # คำนวณ Margin ที่ต้องการ
                if margin_type == "isolated":
                    iso_margin = safe_float(pos.get('isolatedWallet', 0))
                    if side == "LONG":
                        margin_needed_safe = (entry_p - target_liq_safe) * p_amt
                    else:
                        margin_needed_safe = (target_liq_safe - entry_p) * p_amt
                    top_up_safe = max(0.0, margin_needed_safe - iso_margin)
                else:
                    # 🧠 สูตร Cross Margin เฉพาะทาง (อิงตามหลักการของคุณ):
                    # กำไร (UnPnl > 0) จะไม่ถูกนำมารวบหลักประกันเพื่อป้องกันความเสี่ยง
                    pnl_for_collateral = min(0.0, pnl) # ถ้ากำไรให้เป็น 0, ถ้าขาดทุนให้ติดลบจริง
                    effective_avail = a_bal # Available Balance จาก API รวม UnPnl ติดลบมาให้แล้วตามมาตรฐานกระดาน
                    
                    if side == "LONG":
                        # ส่วนต่างมาร์จิ้นที่ขาดหายไปเพื่อให้ Liq ไปถึงเป้าหมาย
                        margin_gap = (entry_p - target_liq_safe) * p_amt
                        top_up_safe = max(0.0, margin_gap - effective_avail)
                    else:
                        margin_gap = (target_liq_safe - entry_p) * p_amt
                        top_up_safe = max(0.0, margin_gap - effective_avail)
                
                # --- ส่วนที่ 2: เติมเพื่อหลุดดอยไว (Recovery Target 1% BE) ---
                target_be = cur_p * (1.008 if side == "LONG" else 0.992)
                if side == "LONG":
                    target_entry = target_be / ((1 + self.maker_fee) / (1 - self.taker_fee))
                    denom = (1 - target_entry/cur_p)
                    top_up_recovery = (p_amt * (target_entry - entry_p) / denom) if abs(denom) > 1e-6 else 0.0
                else:
                    target_entry = target_be / ((1 - self.maker_fee) / (1 + self.taker_fee))
                    denom = (target_entry/cur_p - 1)
                    top_up_recovery = (p_amt * (entry_p - target_entry) / denom) if abs(denom) > 1e-6 else 0.0

                top_up_recovery = max(0.0, top_up_recovery - a_bal)
                
                # คำนวณจำนวนไม้
                base_lot_margin = (max(200.0, min((w_bal * 0.8 / 5) * self.leverage, 500.0))) / self.leverage
                layers_safe = math.ceil(top_up_safe / base_lot_margin) if base_lot_margin > 0 else 0
                layers_recovery = math.ceil(top_up_recovery / base_lot_margin) if base_lot_margin > 0 else 0

                msg += f"🛡️ *Safety Layer ({buffer_rate*100:.0f}% Buffer):*\n"
                if top_up_safe > 0:
                    msg += f"└ เติมเพิ่ม: `${top_up_safe:,.2f}` (~ `{layers_safe}` ไม้)\n"
                else:
                    msg += f"└ ✅ ปลอดภัย (ห่างเป้าหมาย {buffer_rate*100:.0f}%)\n"
                
                msg += f"\n🚀 *Recovery Layer (BE 1.0%):*\n"
                if top_up_recovery > 0:
                    msg += f"└ เติมเพิ่ม: `${top_up_recovery:,.2f}` (~ `{layers_recovery}` ไม้)\n"
                else:
                    msg += f"└ ✅ ใกล้หลุดดอยแล้ว ไม่ต้องเติมเพิ่ม\n"
                
                msg += "━━━━━━━━━━━━━━━━━━\n"
                msg += "⚠️ *Calculated Logic:* ใช้ข้อมูล Wallet Balance (WB) และ Total Maint. Margin (TMM) แบบ Real-time ตรวจสอบความถูกต้องก่อนเติมเงินจริงครับ"

            await self.tg.send_message(msg)
            
        except Exception as e:
            logger.error(f"Exit Analysis Error: {e}")

    async def send_risk_scan(self):
        """🔍 สแกนความเสี่ยง: ตรวจสอบสุขภาพพอร์ตภาพรวม (Portfolio Health Scan)"""
        try:
            acc = await self._get_cached_account()
            if not acc:
                await self.tg.send_message("⚠️ ไม่สามารถดึงข้อมูลพอร์ตได้ในขณะนี้")
                return

            # ดึงข้อมูลจากกระเป๋า USDT
            usdt = next((a for a in acc['assets'] if a['asset'] == 'USDT'), None)
            w_bal = safe_float(usdt['walletBalance']) if usdt else 0.0
            a_bal = safe_float(usdt['availableBalance']) if usdt else 0.0
            m_bal = safe_float(usdt['marginBalance']) if usdt else 0.0
            mm = safe_float(usdt['maintMargin']) if usdt else 0.0
            m_ratio = (mm / m_bal) * 100 if m_bal > 0 else 0.0
            unpnl = safe_float(usdt['unrealizedProfit']) if usdt else 0.0

            # 🌡️ กำหนดระดับความปลอดภัย
            status_icon = "✅"
            status_text = "SAFE (ปลอดภัยดีมาก)"
            if m_ratio >= 90:
                status_icon = "🚨"
                status_text = "CRITICAL (เข้าเขตอันตราย!)"
            elif m_ratio >= 80:
                status_icon = "⚠️"
                status_text = "CAUTION (ระวังเป็นพิเศษ)"
            elif m_ratio >= 50:
                status_icon = "🟡"
                status_text = "MODERATE (เริ่มลดความปลอดภัย)"

            # 📏 หาเหรียญที่เสี่ยงที่สุด
            pos_btc = next((p for p in acc['positions'] if p['symbol'] == self.symbol), None)
            liq_p = safe_float(pos_btc.get('liquidationPrice', 0)) if pos_btc else 0.0
            cur_p = self.current_price
            liq_dist = ((cur_p - liq_p) / cur_p * 100) if cur_p > 0 and liq_p > 0 else 100.0

            msg = f"{status_icon} *PORTFOLIO RISK SCAN*\n"
            msg += "━━━━━━━━━━━━━━━━━━\n"
            msg += f"📊 *Margin Ratio:* `{m_ratio:.2f}%`\n"
            msg += f"🛡️ *Health Status:* `{status_text}`\n"
            msg += "━━━━━━━━━━━━━━━━━━\n"
            msg += f"💰 *Account Equity:* `${m_bal:,.2f}`\n"
            msg += f"💵 *Avaliable Buffer:* `${a_bal:,.2f}`\n"
            msg += f"📈 *Current UNPNL:* `${unpnl:+.2f} USDT`\n"
            msg += "━━━━━━━━━━━━━━━━━━\n"
            
            if liq_p > 0:
                msg += f"🎯 *Liquidation Insight ({self.symbol}):*\n"
                msg += f"└ Liq. Price: `${liq_p:,.2f}`\n"
                msg += f"└ ห่างจากราคาจริง: `{liq_dist:.2f}%`\n"
                
                # ⚖️ คำนวณ Margin Equilibrium: ทนกราฟวิ่งผิดทางได้อีกกี่เหรียญ?
                p_amt = abs(safe_float(pos_btc.get('positionAmt', 0)))
                if p_amt > 0:
                    buffer_usd = a_bal / p_amt
                    msg += f"└ *Equilibrium:* ทนได้อีก `${buffer_usd:,.2f}` (ราคาเหรียญ)\n"

            msg += "━━━━━━━━━━━━━━━━━━\n"
            msg += f"🕒 _Updated: {datetime.now().strftime('%H:%M:%S')}_"

            await self.tg.send_message(msg)
        except Exception as e:
            logger.error(f"Risk Scan Error: {e}")
            await self.tg.send_message("❌ ไม่สามารถประมวลผลการสแกนความเสี่ยงได้ในขณะนี้")

    async def hourly_alert_task(self):
        """ส่งรายงานวิเคราะห์ทางออกอัตโนมัติทุก 1 ชั่วโมง ถ้ามี Position ค้างอยู่"""
        while True:
            try:
                # รอ 1 ชั่วโมง (3600 วินาที)
                await asyncio.sleep(3600)
                
                # เช็คว่ามี Position ไหม
                acc = await self._get_cached_account()
                if acc:
                    pos = next((p for p in acc.get('positions', []) if p['symbol'] == self.symbol), None)
                    p_amt = abs(safe_float(pos['positionAmt'])) if pos else 0.0
                    if p_amt > 0:
                        await self.send_exit_analysis(is_auto=True)
            except Exception as e:
                logger.error(f"Hourly Alert Task Error: {e}")
                await asyncio.sleep(60)

    async def set_pause(self, s):
        self.gl_paused = s
        await self.tg.send_message(f"🆗 {'หยุด' if s else 'เริ่ม'}รันแล้วครับ")

    # ─────────────────────────────────────────────────────────
    # 📚 LOCAL ORDER BOOK SYNC (Binance standard)
    # ─────────────────────────────────────────────────────────
    async def _sync_local_order_book(self):
        """โหลด Snapshot และ sync LOB ตามมาตรฐาน Binance Futures"""
        try:
            snap = await self.client_gl.get_order_book(self.symbol, limit=1000)
            if not snap or 'lastUpdateId' not in snap:
                return
            self._lob_last_update_id = snap['lastUpdateId']
            self._lob_bids = {float(p): float(q) for p, q in snap['bids']}
            self._lob_asks = {float(p): float(q) for p, q in snap['asks']}
            # เคลียร์ buffer events ที่ u < lastUpdateId ออก
            # กรอง events ที่ u > lastUpdateId เท่านั้น (ตาม Binance standard)
            valid = [e for e in self._lob_buffer if e['u'] > self._lob_last_update_id]
            for e in valid:
                self._apply_lob_event(e)
            self._lob_buffer.clear()
            # ใช้ u ของ event ล่าสุดที่ apply เป็น prev_u (ไม่ใช่ snapshot lastUpdateId)
            self._lob_prev_u = valid[-1]['u'] if valid else self._lob_last_update_id
            self._lob_ready = True
            logger.info(f"📚 Local Order Book synced. lastUpdateId={self._lob_last_update_id}")
        except Exception as e:
            logger.error(f"LOB Sync Error: {e}")

    def _apply_lob_event(self, event: dict):
        """อัปเดต LOB จาก depth event — qty=0 ลบออก"""
        for p, q in event.get('b', []):
            price, qty = float(p), float(q)
            if qty == 0:
                self._lob_bids.pop(price, None)
            else:
                self._lob_bids[price] = qty
        for p, q in event.get('a', []):
            price, qty = float(p), float(q)
            if qty == 0:
                self._lob_asks.pop(price, None)
            else:
                self._lob_asks[price] = qty

    async def _on_depth_event(self, event: dict):
        """รับ raw depth event จาก WebSocket ตรวจ sequence ก่อน apply"""
        U = event.get('U', 0)
        u = event.get('u', 0)
        pu = event.get('pu', 0)

        if not self._lob_ready:
            # สะสม events ใน buffer และ apply ทับกันไปเลย (ไม่รอ REST snapshot)
            # แก้ปัญหา Windows latency ที่ทำให้ REST ช้ากว่า WS เสมอ
            self._apply_lob_event(event)
            self._lob_buffer.append(event)
            if len(self._lob_buffer) >= 10:
                self._lob_prev_u = u
                self._lob_ready = True
                self._lob_buffer.clear()
                logger.info(f"📚 LOB ready (event-based, no REST snapshot needed). u={u}")
            return

        # ตรวจ continuity: pu ต้องเท่ากับ prev_u
        if pu != self._lob_prev_u:
            if pu < self._lob_prev_u:
                return  # stale event, skip silently
            # gap เล็กน้อย (< 50k update IDs) → apply ต่อได้เลย ไม่ต้อง re-sync
            if (pu - self._lob_prev_u) < 50000:
                self._lob_prev_u = pu
            else:
                logger.warning(f"⚠️ LOB large gap ({pu - self._lob_prev_u:,}). Resetting...")
                self._lob_ready = False
                self._lob_bids.clear()
                self._lob_asks.clear()
                self._lob_buffer.clear()
                return

        self._apply_lob_event(event)
        self._lob_prev_u = u

    # ─────────────────────────────────────────────────────────
    # 📅 MAX DAILY LOSS & RAPID-FIRE THROTTLE
    # ─────────────────────────────────────────────────────────
    def _reset_daily_loss_if_new_day(self):
        """Reset daily loss counter เมื่อถึงวันใหม่ (UTC)"""
        import datetime
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        if self._daily_loss_date != today:
            self._daily_loss_date  = today
            self._daily_loss_total = 0.0
            self._daily_kill_active = False
            logger.info(f"📅 Daily Loss Reset: วันใหม่ {today} — เคาน์เตอร์รีเซ็ตแล้ว")

    def _record_realized_pnl(self, pnl: float):
        """บันทึก Realized PnL หลังปิด Position — เรียกจาก _execute_trade is_close=True"""
        self._reset_daily_loss_if_new_day()
        self._daily_loss_total += pnl
        logger.info(f"📅 Daily PnL: ${self._daily_loss_total:.2f} (added ${pnl:+.2f})")
        if not self._daily_kill_active and self._daily_loss_total <= self.MAX_DAILY_LOSS:
            self._daily_kill_active = True
            logger.critical(f"📅 MAX DAILY LOSS HIT: ${self._daily_loss_total:.2f} ≤ ${self.MAX_DAILY_LOSS} → หยุดเทรดทั้งวัน")
            asyncio.ensure_future(self.tg.send_message(
                f"📅 *MAX DAILY LOSS — TRADING HALTED*\n"
                f"ขาดทุนสะสมวันนี้: `${self._daily_loss_total:.2f}`\n"
                f"เกินเกณฑ์: `${self.MAX_DAILY_LOSS}`\n"
                f"⛔ บอทหยุดเทรดจนถึงเที่ยงคืน UTC ครับ"
            ))

    def _check_order_throttle(self) -> bool:
        """Rapid-Fire Throttle: บล็อกเมื่อส่ง order > THROTTLE_MAX_ORDERS ใน THROTTLE_WINDOW_SEC
        Returns True ถ้าถูก throttle (ห้ามส่ง order)
        """
        now = time.time()
        # ลบ timestamp ที่พ้น window ออก
        self._order_timestamps = [t for t in self._order_timestamps if now - t <= self.THROTTLE_WINDOW_SEC]
        if len(self._order_timestamps) >= self.THROTTLE_MAX_ORDERS:
            if not self._throttle_blocked:
                self._throttle_blocked = True
                oldest = self._order_timestamps[0] if self._order_timestamps else now
                resume_in = int(self.THROTTLE_WINDOW_SEC - (now - oldest))
                logger.warning(f"⚡ RAPID-FIRE THROTTLE: {len(self._order_timestamps)} orders ใน {self.THROTTLE_WINDOW_SEC}s → บล็อก {resume_in}s")
                asyncio.ensure_future(self.tg.send_message(
                    f"⚡ *RAPID-FIRE THROTTLE ACTIVE*\n"
                    f"ส่ง `{len(self._order_timestamps)}` orders ใน `{self.THROTTLE_WINDOW_SEC}s`\n"
                    f"บล็อกอีก ~`{resume_in}s` เพื่อป้องกันระบบทำงานผิดปกติ"
                ))
            return True
        self._throttle_blocked = False
        return False

    # ─────────────────────────────────────────────────────────
    # 📐 DYNAMIC LOT SIZING & VOLATILITY GATE
    # ─────────────────────────────────────────────────────────
    def _get_lot_scale(self) -> float:
        """คืนค่า multiplier ของ Lot ตาม Layer ปัจจุบัน (Downward Protection)
        Layer 1-5: 100% | 6-7: 75% | 8-9: 50% | 10+: 30%
        """
        layer = self.active_layers
        scale = 1.0
        for threshold, s in sorted(self.LOT_SCALE_BY_LAYER.items(), reverse=True):
            if layer >= threshold:
                scale = s
                break
        return scale

    def _check_volatility_gate(self, cur_p: float) -> bool:
        """VolatilityGate: คำนวณ σ/μ ratio จาก price buffer
        Returns True ถ้าปลอดภัย (CHOPPY), False ถ้าบล็อก (TRENDING)

        σ = std dev ของ price changes (ความผันผวน)
        μ = mean absolute MA change (ทิศทาง Drift)
        σ/μ > VGATE_SIGMA_MU_MIN = CHOPPY (OK) | < threshold = TRENDING (BLOCK)
        """
        self._vgate_price_history.append(cur_p)
        if len(self._vgate_price_history) > 20:
            self._vgate_price_history.pop(0)

        hist = self._vgate_price_history
        if len(hist) < 10:
            return True  # ข้อมูลยังน้อย → ไม่บล็อก

        # คำนวณ σ: std dev ของ log returns
        returns = [(hist[i] - hist[i-1]) / hist[i-1] for i in range(1, len(hist))]
        mean_r = sum(returns) / len(returns)
        sigma = (sum((r - mean_r)**2 for r in returns) / len(returns)) ** 0.5

        # คำนวณ μ: mean absolute MA change (5-bar MA)
        ma = [sum(hist[max(0,i-4):i+1]) / min(5, i+1) for i in range(len(hist))]
        mu = sum(abs(ma[i] - ma[i-1]) / ma[i-1] for i in range(1, len(ma))) / max(len(ma)-1, 1)

        ratio = sigma / mu if mu > 1e-8 else 999.0

        was_blocked = self._vgate_blocked
        if ratio >= self.VGATE_SIGMA_MU_MIN:
            self._vgate_safe_count += 1
            if self._vgate_safe_count >= self.VGATE_REARM_BARS:
                self._vgate_blocked = False
        else:
            self._vgate_safe_count = 0
            self._vgate_blocked = True

        if was_blocked and not self._vgate_blocked:
            logger.info(f"✅ VolatilityGate Re-armed: σ/μ={ratio:.1f} ≥ {self.VGATE_SIGMA_MU_MIN} ({self.VGATE_REARM_BARS} bars ติดต่อกัน)")
        elif not was_blocked and self._vgate_blocked:
            logger.warning(f"🌡️ VolatilityGate BLOCKED: σ/μ={ratio:.2f} < {self.VGATE_SIGMA_MU_MIN} → Trending detected")

        return not self._vgate_blocked

    # ─────────────────────────────────────────────────────────
    # 🔴 KILL SWITCH
    # ─────────────────────────────────────────────────────────
    def _get_dynamic_cooldown(self) -> int:
        """🧭 Dynamic Cooldown ตาม Regime (Priority 2 Active)
        CHOPPY   → 60s  (Liquidity กลับเร็ว, ไม่เสียโอกาส)
        TRENDING → 300s (Data n=27: DUMP 37% / FLAT 52% / BOUNCE 11% → รอ Momentum หมดแรง)
        VOLATILE → 240s (Capital Preservation สูงสุด)
        WARMING  → 60s  (buffer ยังน้อย ใช้ค่าปลอดภัย)
        """
        regime, _ = self._get_regime()
        cooldown_map = {
            "CHOPPY":   self.KS_COOLDOWN_CHOPPY,
            "TRENDING": self.KS_COOLDOWN_TRENDING,
            "VOLATILE": self.KS_COOLDOWN_VOLATILE,
            "WARMING":  self.KS_COOLDOWN_WARMING,
        }
        return cooldown_map.get(regime, self.KS_COOLDOWN_CHOPPY)

    async def _check_kill_switch(self, inst_vol: float, client) -> bool:
        """ตรวจสอบเงื่อนไข Kill Switch ทุก loop
        Returns True ถ้า kill switch active (ห้ามเทรด)
        """
        now = time.time()
        regime, rng = self._get_regime()
        
        # 1. Update CUSUM Breakout
        if len(self.price_buffer) >= 2:
            import numpy as np
            price_diffs = np.diff(list(self.price_buffer))
            vol = np.std(price_diffs)
            z = price_diffs[-1] / (vol if vol > 1e-8 else 1.0)
            self._cusum_pos = max(0, self._cusum_pos + z - self.CUSUM_K)
            self._cusum_neg = max(0, self._cusum_neg - z - self.CUSUM_K)
            
            # CUSUM Fires -> บังคับ Trigger Block (TRENDING)
            if not self._kill_switch and (self._cusum_pos > self.CUSUM_H or self._cusum_neg > self.CUSUM_H):
                self._kill_switch = True
                self._kill_time = now
                self._kill_reason = f"CUSUM Breakout! (Trend detected, Pos:{self._cusum_pos:.1f} Neg:{self._cusum_neg:.1f})"
                self._cusum_pos = 0.0
                self._cusum_neg = 0.0
                dynamic_cd = self._get_dynamic_cooldown()
                logger.critical(f"🚨 CUSUM BLOCK TRIGGERED: {self._kill_reason} | Cooldown {dynamic_cd}s")
                await self.tg.send_message(f"🚨 *CUSUM Breakout Block!*\nตรวจพบตลาดกำลังสร้างเทรนด์ (Trending Regime)\nเหตุผล: `{self._kill_reason}`\nระยะเวลาพัก: `{dynamic_cd}s`\nคำสั่งที่เปิดอยู่จะถูกยกเลิกทั้งหมดเพื่อป้องกันรับมีด!")
                await self._cancel_all_open_orders(client)
                return True

        # รอ cooldown ก่อนเปิดใหม่ — ใช้ Dynamic Cooldown ตาม Regime ปัจจุบัน
        if self._kill_switch:
            dynamic_cd = self._get_dynamic_cooldown()
            # Full Cooldown Recovery (ต้องพ้นระยะเวลา + ตลาดเป็น CHOPPY ถึงจะปลดให้)
            if (now - self._kill_time) > dynamic_cd:
                if regime == "CHOPPY" or self.KS_COOLDOWN_VOLATILE > 0: # ปลดถ้าตลาดอยู่ในเกณฑ์
                    self._kill_switch = False
                    self._kill_reason = ""
                    self._vol_spike_count = 0
                    self._safe_bar_count = 0
                    # 🛡️ Restore last_buy_price จาก entry_p เสมอ (ไม่ว่าจะ 0 หรือไม่)
                    # entry_p คือราคาเฉลี่ยจริง → next_buy_price ถอยห่างตาม grid_step
                    # ถ้าปล่อยเป็น cur_p → next_buy_price ≈ cur_p → DCA trigger ทันที (bug)
                    try:
                        acc_ks = await self._get_cached_account()
                        pos_ks = next((p for p in acc_ks.get('positions', []) if p['symbol'] == self.symbol), None) if acc_ks else None
                        entry_ks = safe_float(pos_ks['entryPrice']) if pos_ks else 0.0
                        if entry_ks > 0:
                            self.last_buy_price = entry_ks
                            logger.info(f"🛡️ KS Release: last_buy_price = ${entry_ks:,.2f} (entry_p)")
                    except Exception:
                        pass
                    # 🛡️ Cooldown 30s หลัง KS release ป้องกัน DCA ทันที
                    # ตั้ง last_close_time ให้เหมือนเพิ่งปิดไป 30 วินาที → รอ cooldown อีก 30s
                    self.last_close_time = now - 30.0
                    logger.info(f"✅ Full Cooldown Recovery released. Regime={regime}")
                    await self.tg.send_message(f"✅ *Kill Switch Released:* หมดเวลายับยั้งและตลาดฟื้นตัวอยู่ในเกณฑ์ที่ตั้งไว้ครับ\n🧭 Regime: `{regime}`")
            else:
                remaining = int(dynamic_cd - (now - self._kill_time))
                logger.debug(f"🔴 Kill Switch active (Block). Resume in {remaining}s (Regime: {regime})")
                return True

        # 2. ป้องกัน Noise และสวิงใน Choppy ด้วย OFI Filter 
        # (หยุดพักชั่วคราว รอคอนเฟิร์ม N แท่ง)
        if regime == "CHOPPY" and self.active_layers == 0:
            if abs(self._ofi_score) > 0.35: # มีแรงไม่สมดุลชัดเจน (Stabilizer)
                self._safe_bar_count += 1
            else:
                self._safe_bar_count = 0 # เป็นแค่ Noise ให้ Reset เคาต์ดาวน์

            if self._safe_bar_count < 3:
                self._cooldown_state = "PAUSED"
                logger.debug(f"🟡 CHOPPY PAUSED: รอ OFI ยืนยัน {self._safe_bar_count}/3 แท่งต่อเนื่อง ป้องกันสัญญาณหลอก")
                return True
            else:
                self._cooldown_state = "READY"

        # ตรวจ volatility spike
        if inst_vol > self.KS_VOL_THRESHOLD:
            self._vol_spike_count += 1
        else:
            self._vol_spike_count = max(0, self._vol_spike_count - 1)

        if self._vol_spike_count >= self.KS_VOL_SPIKE_MAX:
            self._kill_switch = True
            self._kill_time = now
            dynamic_cd = self._get_dynamic_cooldown()
            regime, _ = self._get_regime()
            self._kill_reason = f"Volatility spike {inst_vol:.2f}% > {self.KS_VOL_THRESHOLD}% | Regime={regime}"
            logger.critical(f"🔴 KILL SWITCH TRIGGERED: {self._kill_reason} | Cooldown {dynamic_cd}s")
            await self.tg.send_message(
                f"🔴 *KILL SWITCH TRIGGERED!*\n"
                f"เหตุผล: `{self._kill_reason}`\n"
                f"🧭 Regime: `{regime}` → Cooldown: `{dynamic_cd}s`\n"
                f"คำสั่งที่เปิดอยู่จะถูกยกเลิกทั้งหมด"
            )
            await self._cancel_all_open_orders(client)
            return True

        return False

    async def _cancel_all_open_orders(self, client):
        """ยกเลิก open orders ทั้งหมด (risk-reducing เท่านั้น)"""
        try:
            res = await client._request("DELETE", "/fapi/v1/allOpenOrders",
                                        signed=True, params={"symbol": self.symbol}, priority=0)
            logger.info(f"🗑️ All open orders cancelled: {res}")
        except Exception as e:
            logger.error(f"Cancel Orders Error: {e}")

    # ─────────────────────────────────────────────────────────
    # 📓 FLIP LOGGER — Self-Learning Pattern Engine
    # ─────────────────────────────────────────────────────────
    def _process_flip_outcomes(self):
        """เรียกทุก loop — ตรวจว่า pending outcome ไหนครบ 5 นาทีแล้ว
        บันทึก outcome_delta และ label (BOUNCE/DUMP/FLAT) ลงใน flip_log
        """
        if not self._flip_pending_outcome:
            return
        now = time.time()
        still_pending = []
        for pending in self._flip_pending_outcome:
            if now >= pending["check_at"] and self.current_price > 0:
                entry_ref = pending["entry_ref"]
                entry_price = pending["entry_price"]
                outcome_price = self.current_price
                delta_pct = (outcome_price - entry_price) / entry_price * 100
                # Label: BOUNCE = ราคาขึ้น ≥ 0.1%, DUMP = ลง ≥ 0.1%, FLAT = อยู่ในกรอบ
                if delta_pct >= 0.1:
                    label = "BOUNCE"
                elif delta_pct <= -0.1:
                    label = "DUMP"
                else:
                    label = "FLAT"
                entry_ref["outcome_price"] = round(outcome_price, 2)  # pyre-ignore
                entry_ref["outcome_delta"] = round(delta_pct, 4)  # pyre-ignore
                entry_ref["outcome_label"] = label
                logger.info(
                    f"📓 Flip Outcome [{label}]: "
                    f"Regime={entry_ref['regime']} | "
                    f"Price {entry_price:,.2f} → {outcome_price:,.2f} ({delta_pct:+.3f}%) | "
                    f"TradeOBI={entry_ref['trade_obi']:+.2f}"
                )
            else:
                still_pending.append(pending)
        self._flip_pending_outcome = still_pending
        self._flip_log_save()

    def _flip_log_save(self):
        """บันทึก flip_log ลงไฟล์ JSON — เฉพาะ events ที่ outcome ครบแล้ว"""
        try:
            import json
            completed = [e for e in self._flip_log if e["outcome_label"] is not None]
            with open(self._flip_log_path, "w") as f:
                json.dump(completed, f)
        except Exception as e:
            logger.warning(f"📓 Flip log save error: {e}")

    def _flip_log_load(self):
        """โหลด flip_log จากไฟล์ JSON เมื่อ restart"""
        try:
            import json
            if not os.path.exists(self._flip_log_path):
                return
            with open(self._flip_log_path, "r") as f:
                data = json.load(f)
            for e in data:
                self._flip_log.append(e)
            logger.info(f"📓 Flip log loaded: {len(data)} events จากไฟล์")
        except Exception as e:
            logger.warning(f"📓 Flip log load error: {e}")

    def _trailing_state_save(self):
        """บันทึก trailing state ลงไฟล์ — ป้องกัน peak_price หาย เมื่อ restart"""
        try:
            import json
            state = {"trailing_active": self.trailing_active, "peak_price": self.peak_price}
            with open(self._trailing_state_path, "w") as f:
                json.dump(state, f)
        except Exception as e:
            logger.warning(f"📌 Trailing state save error: {e}")

    def _trailing_state_load(self):
        """โหลด trailing state เมื่อ restart — ถ้าเคย activate ก่อน restart จะทำงานต่อได้"""
        try:
            import json
            if not os.path.exists(self._trailing_state_path):
                return
            with open(self._trailing_state_path, "r") as f:
                state = json.load(f)
            if state.get("trailing_active") and state.get("peak_price", 0) > 0:
                self.trailing_active = True
                self.peak_price = state["peak_price"]
                logger.info(f"📌 Trailing state loaded: active=True, peak=${self.peak_price:,.0f}")
        except Exception as e:
            logger.warning(f"📌 Trailing state load error: {e}")

    def _get_flip_stats(self) -> str:
        """สรุปสถิติ Flip Log — Win Rate แยกตาม Regime
        คืน string สำหรับแสดงใน Dashboard หรือ Telegram
        """
        completed = [e for e in self._flip_log if e["outcome_label"] is not None]
        if not completed:
            return "📓 Flip Log: ยังไม่มีข้อมูลครบ 5 นาทีครับ"

        # รวมสถิติแยกตาม Regime
        from collections import defaultdict
        stats: dict = defaultdict(lambda: {"BOUNCE": 0, "DUMP": 0, "FLAT": 0, "total": 0})
        for e in completed:
            r = e["regime"]
            stats[r][e["outcome_label"]] += 1
            stats[r]["total"] += 1

        # นับ KS triggered vs bypassed
        ks_fired = sum(1 for e in completed if e.get("ks_triggered", True))
        ks_bypass = len(completed) - ks_fired

        lines = [f"📓 *Flip Log Stats* ({len(completed)} events | KS fired={ks_fired} bypass={ks_bypass})"]
        for regime, s in sorted(stats.items()):
            total = s["total"]
            bounce_rate = s["BOUNCE"] / total * 100
            dump_rate = s["DUMP"] / total * 100
            flat_rate = s["FLAT"] / total * 100
            lines.append(
                f"  `{regime}` → "
                f"🟢 BOUNCE {bounce_rate:.0f}% | "
                f"🔴 DUMP {dump_rate:.0f}% | "
                f"⚪ FLAT {flat_rate:.0f}% "
                f"(n={total})"
            )

        # แสดง 3 events ล่าสุด
        lines.append("─ ล่าสุด 3 events ─")
        for e in list(completed)[-3:]:  # pyre-ignore
            t = datetime.fromtimestamp(e["time"]).strftime("%H:%M")
            lines.append(
                f"  `{t}` [{e['regime']}] {e['outcome_label']} "
                f"{e['outcome_delta']:+.3f}% | "
                f"OBI {e['obi_before']:+.2f}→{e['obi_after']:+.2f} "
                f"T:{e['trade_obi']:+.2f}"
            )
        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────
    # ⚖️ INVENTORY SKEW CONTROL
    # ─────────────────────────────────────────────────────────
    def _check_inventory_skew(self, p_amt: float, entry_p: float, cur_p: float, margin: float) -> str:
        """ตรวจสอบสถานะ position ว่า overexposed หรือ toxic
        Returns: 'ok' | 'overexposed' | 'toxic'
        """
        if abs(p_amt) == 0 or entry_p <= 0:
            return 'ok'

        # ตรวจ overexposed: ไม้มากเกินไป
        if self.active_layers > self.INV_MAX_LAYERS:
            return 'overexposed'

        # ตรวจ toxic: ขาดทุนเกิน % ของ margin
        if margin > 0:
            loss = (entry_p - cur_p) * abs(p_amt)
            loss_pct = (loss / margin) * 100
            if loss_pct > self.INV_MAX_LOSS_PCT:
                return 'toxic'

        return 'ok'

    def _obi_confirmed(self) -> bool:
        """OBI น่าเชื่อถือก็ต่อเมื่อ OFI ยืนยันทิศทางเดียวกัน (ป้องกัน Spoof)"""
        if self._obi_last_update == 0:
            return True  # ยังไม่มีข้อมูล ให้ผ่าน
        obi = self._obi_score
        ofi = self._ofi_score
        # ทั้ง OBI และ OFI ต้องชี้ทิศเดียวกัน หรือ OBI เป็นกลาง
        if abs(obi) < 0.3:
            return True
        return (obi > 0 and ofi > -0.35) or (obi < 0 and ofi < 0.35)

    def _get_regime(self) -> tuple:
        """[Read-Only Diagnostics] ตรวจจับสภาวะตลาดจาก price_buffer (Sigma/Mu Ratio)"""
        try:
            if len(self.price_buffer) < 2:
                return "WARMING", 0.0
            
            import numpy as np
            price_diffs = np.diff(list(self.price_buffer))
            if len(price_diffs) == 0:
                return "WARMING", 0.0

            drift = np.mean(price_diffs)
            vol = np.std(price_diffs)
            
            lo = min(self.price_buffer)
            rng = (max(self.price_buffer) - lo) / lo * 100 if lo > 0 else 0.0

            # Volatility vs Drift Ratio
            if abs(drift) > 1e-8:
                ratio = vol / abs(drift)
            else:
                ratio = float('inf')

            # กำหนด Regime
            if ratio > 2.5:
                regime = "CHOPPY"
            elif rng > 0.8:
                regime = "VOLATILE"
            else:
                regime = "TRENDING"
                
            return regime, rng
        except Exception:
            return "ERROR", 0.0

    def _get_whale_signal(self) -> str:
        """Whale Signal จาก OBI + OFI + Spoof Filter"""
        if self._obi_last_update == 0 or (time.time() - self._obi_last_update) > 30:
            return "`กำลังเชื่อมต่อ Order Book...`"

        obi = self._obi_score
        obi_deep = self._obi_deep
        ofi = self._ofi_score
        bid = self._obi_bid_vol / 1_000_000
        ask = self._obi_ask_vol / 1_000_000
        confirmed = self._obi_confirmed()
        conf_tag = "" if confirmed else " ⚠️`(OFI ไม่ยืนยัน)`"

        # Deep Spoof hint: OBI_flat สูง แต่ OBI_deep ต่ำ = กำแพงอยู่ไกล mid → สัญญาณ Spoof เพิ่มเติม
        deep_gap = obi - obi_deep  # บวก = flat สูงกว่า deep = wall ไกล mid
        spoof_hint = " 🔍`(Deep diverge — wall ไกล mid)`" if deep_gap > 0.25 else ""

        if obi >= 0.6:
            signal = f"🐳 *STRONG BUY* | OBI_flat `{obi:+.2f}` OBI_deep `{obi_deep:+.2f}` OFI `{ofi:+.2f}` | `${bid:.1f}M` vs `${ask:.1f}M`{conf_tag}{spoof_hint}"
        elif obi >= 0.3:
            signal = f"🟢 *Buy Pressure* | OBI_flat `{obi:+.2f}` OBI_deep `{obi_deep:+.2f}` OFI `{ofi:+.2f}` | `${bid:.1f}M` vs `${ask:.1f}M`{conf_tag}{spoof_hint}"
        elif obi <= -0.6:
            signal = f"🐳 *STRONG SELL* | OBI_flat `{obi:+.2f}` OBI_deep `{obi_deep:+.2f}` OFI `{ofi:+.2f}` | `${bid:.1f}M` vs `${ask:.1f}M`{conf_tag}{spoof_hint}"
        elif obi <= -0.3:
            signal = f"🔴 *Sell Pressure* | OBI_flat `{obi:+.2f}` OBI_deep `{obi_deep:+.2f}` OFI `{ofi:+.2f}` | `${bid:.1f}M` vs `${ask:.1f}M`{conf_tag}{spoof_hint}"
        else:
            signal = f"⚖️ *Balanced* | OBI_flat `{obi:+.2f}` OBI_deep `{obi_deep:+.2f}` OFI `{ofi:+.2f}` | `${bid:.1f}M` vs `${ask:.1f}M`{spoof_hint}"

        # Whale Walls (กรอง Spoof ออกแล้ว) + Tiered display
        tier_icon = {"mega": "🔴", "strong": "🟠", "watch": "🟡"}
        tier_label = {"mega": "MEGA", "strong": "STRONG", "watch": "WATCH"}
        walls = []

        if self._whale_bid_walls:
            top = max(self._whale_bid_walls, key=lambda x: x[1])
            p, q, tier = top[0], top[1], top[2] if len(top) > 2 else "watch"
            icon = tier_icon.get(tier, "🟡")
            label = tier_label.get(tier, "WATCH")
            walls.append(f"{icon} Buy Wall [{label}] `${p:,.0f}` ({q:.2f} BTC)")

        if self._whale_ask_walls:
            top = min(self._whale_ask_walls, key=lambda x: x[0])
            p, q, tier = top[0], top[1], top[2] if len(top) > 2 else "watch"
            icon = tier_icon.get(tier, "🟡")
            label = tier_label.get(tier, "WATCH")
            walls.append(f"{icon} Sell Wall [{label}] `${p:,.0f}` ({q:.2f} BTC)")

        # สรุป tier count
        all_walls = self._whale_bid_walls + self._whale_ask_walls
        if all_walls:
            n_mega   = sum(1 for w in all_walls if (w[2] if len(w) > 2 else "watch") == "mega")
            n_strong = sum(1 for w in all_walls if (w[2] if len(w) > 2 else "watch") == "strong")
            n_watch  = sum(1 for w in all_walls if (w[2] if len(w) > 2 else "watch") == "watch")
            parts = []
            if n_mega:   parts.append(f"🔴×{n_mega}")
            if n_strong: parts.append(f"🟠×{n_strong}")
            if n_watch:  parts.append(f"🟡×{n_watch}")
            spoof_count = len(self._spoof_prices)
            walls.append(f"🕵️ Walls: {' '.join(parts)} | กรอง {spoof_count} spoof")
        elif self._spoof_prices and len(self._spoof_prices) >= 3:
            walls.append(f"🕵️ Spoof `{len(self._spoof_prices)}` walls (ระวัง!)")

        return signal + ("\n" + " | ".join(walls) if walls else "")

    async def _auto_monitor(self, client, p_amt: float, pnl: float, entry_p: float):
        """🤖 Cipher Monitor Logic ที่รวมอยู่ในบอทโดยตรง — ตัดสินใจเองโดยไม่ต้องพึ่ง Node.js"""
        now = time.time()
        if (now - self._monitor_last_check) < self._monitor_interval:
            return

        self._monitor_last_check = now
        alert = self._monitor_last_alert

        # ---- วิกฤต: เพิ่มความถี่ตรวจสอบ ----
        if self.active_layers >= self.MONITOR_CRITICAL_LAYERS or pnl <= self.MONITOR_MAX_LOSS:
            self._monitor_interval = 60   # เช็คทุก 1 นาที
        else:
            self._monitor_interval = 900  # เช็คทุก 15 นาที

        # ---- Rule 1: กำไรถึงเป้า → ปิดทันที ----
        # 🧠 Dynamic Profit Target: Layer สูง = รอ profit มากขึ้น (คุ้มค่ากับความเสี่ยงที่แบกมา)
        # Layer 1-5: $5 | Layer 6-7: $8 | Layer 8-9: $12 | Layer 10+: $18
        if self.active_layers >= 10:
            dynamic_profit_target = 18.0
        elif self.active_layers >= 8:
            dynamic_profit_target = 12.0
        elif self.active_layers >= 6:
            dynamic_profit_target = 8.0
        else:
            dynamic_profit_target = self.MONITOR_PROFIT_TARGET

        if pnl >= dynamic_profit_target and abs(p_amt) > 0:
            if alert["type"] != "close" or abs(pnl - alert["pnl"]) > 1.0:
                closed_from_layer = self.active_layers
                logger.info(f"🤖 Monitor: กำไร ${pnl:.2f} ถึงเป้า (Layer {closed_from_layer}) → ปิด Position")
                await self.tg.send_message(f"🤖 *AUTO-MONITOR*: กำไร `${pnl:.2f}` ถึงเป้า `${dynamic_profit_target}` (Layer `{closed_from_layer}`) → ปิด Position อัตโนมัติ!")
                side = "SELL" if p_amt > 0 else "BUY"
                await self._execute_trade(client, side, abs(p_amt), True)
                self.last_close_time = time.time()
                self.active_layers, self.trailing_active = 0, False
                self._monitor_last_alert = {"type": None, "pnl": 0.0, "layers": 0}

                # 🔄 Post-TP Auto-Reset: ถ้าปิดจาก Layer สูง (≥6) → reset กลับ NORMAL อัตโนมัติ
                # เพื่อให้รอบใหม่ใช้ Grid Step ปกติ ไม่ใช่ SAFE 3x
                if closed_from_layer >= 6 and self.strategy_mode in ("SAFE", "PROFIT"):
                    self.strategy_mode = "NORMAL"
                    self._equity_kill_active = False
                    self._last_closed_from_high_layer = True  # flag: รอบใหม่ต้องระวังมากขึ้น
                    await self.update_strategy_parameters()
                    await self.tg.send_message(
                        f"✅ *POST-TP AUTO-RESET*: ปิดสำเร็จจาก Layer `{closed_from_layer}` → Reset โหมดกลับ `NORMAL` แล้วครับ\n"
                        f"🆕 รอบใหม่จะเริ่มที่ Layer 1 ด้วย Grid Step ปกติ\n"
                        f"⏳ รอ 5 นาทีและ OBI ยืนยันก่อนเปิดไม้แรก"
                    )
            return

        # ---- Rule 2: ใกล้กำไร → เปลี่ยน PROFIT mode (เฉพาะเมื่อไม้ยังปลอดภัย < 8) ----
        if pnl >= self.MONITOR_NEAR_PROFIT and self.active_layers < self.MONITOR_HIGH_LAYERS and self.strategy_mode != "PROFIT":
            if alert["type"] != "near_profit":
                logger.info(f"🤖 Monitor: กำไร ${pnl:.2f} ใกล้เป้า (ไม้={self.active_layers}) → เปลี่ยน PROFIT mode")
                self.strategy_mode = "PROFIT"
                await self.update_strategy_parameters()
                await self.tg.send_message(f"🤖 *AUTO-MONITOR*: กำไร `${pnl:.2f}` ใกล้เป้า → เปลี่ยนเป็นโหมด `PROFIT` เพื่อปิดไวขึ้น!")
                self._monitor_last_alert = {"type": "near_profit", "pnl": pnl, "layers": self.active_layers}

        # ---- Rule 3: ไม้วิกฤต >= 10 → บังคับ SAFE ----
        elif self.active_layers >= self.MONITOR_CRITICAL_LAYERS and self.strategy_mode != "SAFE":
            if alert["type"] != "critical_layers" or alert["layers"] != self.active_layers:
                logger.warning(f"🤖 Monitor: ไม้ {self.active_layers} ถึงวิกฤต → บังคับ SAFE mode")
                self.strategy_mode = "SAFE"
                await self.update_strategy_parameters()
                await self.tg.send_message(f"🚨 *AUTO-MONITOR*: ไม้ถึง `{self.active_layers}/12` (วิกฤต!) → บังคับโหมด `SAFE` ถอยระยะไม้ห่างขึ้น 50%!")
                self._monitor_last_alert = {"type": "critical_layers", "pnl": pnl, "layers": self.active_layers}

        # ---- Rule 4: ไม้สูง >= 8 → บังคับ SAFE ----
        elif self.active_layers >= self.MONITOR_HIGH_LAYERS and self.strategy_mode == "NORMAL":
            if alert["type"] != "high_layers" or alert["layers"] != self.active_layers:
                logger.warning(f"🤖 Monitor: ไม้ {self.active_layers} สูง → เปลี่ยน SAFE mode")
                self.strategy_mode = "SAFE"
                await self.update_strategy_parameters()
                await self.tg.send_message(f"⚠️ *AUTO-MONITOR*: ไม้ถึง `{self.active_layers}/12` → เปลี่ยนเป็นโหมด `SAFE` เพื่อลดความเสี่ยง!")
                self._monitor_last_alert = {"type": "high_layers", "pnl": pnl, "layers": self.active_layers}

        # ---- Rule 5: ขาดทุนเกิน → เตือน (cooldown 10 นาที ป้องกัน spam) ----
        if pnl <= self.MONITOR_MAX_LOSS:
            if alert["type"] != "max_loss" or abs(pnl - alert["pnl"]) > 10.0:
                logger.warning(f"🤖 Monitor: ขาดทุน ${pnl:.2f} เกินขีดจำกัด!")
                await self.tg.send_message(f"🚨 *AUTO-MONITOR*: ขาดทุน `${pnl:.2f}` เกิน `-${abs(self.MONITOR_MAX_LOSS)}` แล้ว!\nกรุณาพิจารณาปิด Position หรือเติมเงินครับ")
                self._monitor_last_alert = {"type": "max_loss", "pnl": pnl, "layers": self.active_layers}

        # ---- Rule 6: Liq. ใกล้ (< 4%) → เตือนด่วน ----
        if entry_p > 0 and self.current_price > 0:
            acc = await self._get_cached_account()
            pos = next((p for p in acc['positions'] if p['symbol'] == self.symbol), None) if acc else None
            liq_p = safe_float(pos.get('liquidationPrice', 0)) if pos else 0.0
            if liq_p > 0 and self.current_price > 0:
                liq_dist_pct = (self.current_price - liq_p) / self.current_price * 100
                if liq_dist_pct < 4.0:
                    if alert["type"] != "liq_near" or abs(liq_dist_pct - alert.get("pnl", 99)) > 0.5:
                        logger.critical(f"🚨 Monitor: Liq. ใกล้มาก! ห่างแค่ {liq_dist_pct:.1f}%")
                        iso_margin = safe_float(pos.get('isolatedWallet', 0)) if pos else 0.0
                        p_amt_mon = safe_float(pos.get('positionAmt', 0)) if pos else 0.0
                        _usdt_mon = next((a for a in (acc or {}).get('assets', []) if a['asset'] == 'USDT'), None)
                        avail_mon = safe_float(_usdt_mon['availableBalance']) if _usdt_mon else 0.0
                        liq_msg = (
                            f"🚨🚨 *LIQ. DANGER!* 🚨🚨\n"
                            f"ราคา Liq.: `${liq_p:,.0f}` | ห่างแค่ `{liq_dist_pct:.1f}%`\n"
                            f"ราคาปัจจุบัน: `${self.current_price:,.0f}`\n"
                        )
                        if iso_margin > 0 and abs(p_amt_mon) > 0:
                            # คำนวณขั้นต่ำที่ต้องเติมเพื่อให้ห่าง Liq. 5%
                            # target_liq = mark * (1 - 0.05)
                            # target_liq = entry - (margin_new / qty)
                            # margin_new = (entry - target_liq) * qty
                            target_liq = self.current_price * 0.95
                            margin_needed = (entry_p - target_liq) * abs(p_amt_mon)
                            top_up_min = max(0.0, margin_needed - iso_margin)
                            liq_msg += f"─────────────────\n"
                            liq_msg += f"💡 *เติมขั้นต่ำ `${top_up_min:.2f}` เพื่อให้ปลอดภัย 5%*\n"
                            if avail_mon >= 1.0:
                                new_liq_a = entry_p - ((iso_margin + avail_mon) / abs(p_amt_mon))
                                dist_a = (self.current_price - new_liq_a) / self.current_price * 100
                                safe_a = "✅" if dist_a >= 5.0 else "⚠️"
                                liq_msg += f"{safe_a} ถ้าเติมทั้งหมด `${avail_mon:.2f}`: Liq. `${new_liq_a:,.0f}` ห่าง `{dist_a:.2f}%`\n"
                        liq_msg += f"⚡ *กรุณาเติมเงินหรือปิด Position ด่วน!*"
                        await self.tg.send_message(liq_msg)
                        self._monitor_last_alert = {"type": "liq_near", "pnl": liq_dist_pct, "layers": self.active_layers}

async def health_server(tg: TelegramCommander):
    """Mini HTTP server สำหรับรับ Webhook"""
    async def handle(request):
        return web.Response(text="✅ COMMANDER v119.8 Status: Operational")
    
    async def webhook_handler(request):
        try:
            data = await request.json()
            await tg.process_update(data)
            return web.Response(text="OK")
        except Exception as e:
            logger.error(f"Webhook Handler Error: {e}")
            return web.Response(text="Error", status=500)

    app = web.Application()
    app.router.add_get('/', handle)
    app.router.add_get('/health', handle)
    # 🛡️ Endpoint สำหรับ Webhook (ใช้ Token เป็นความลับใน URL)
    app.router.add_post(f'/{tg.token}', webhook_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get('PORT', 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Server active on port {port} with Webhook support")

async def self_ping():
    """ฟังก์ชั่นปลุกบอทไม่ให้หลับ โดยการยิง Request หา URL ตัวเองทุกๆ 5 นาที"""
    url = os.environ.get('EXTERNAL_URL')
    if not url:
        logger.info("ℹ️ EXTERNAL_URL not set. Self-ping idle (Normal for local).")
        return

    logger.info(f"📡 Anti-spin-down active. Target: {url}")
    await asyncio.sleep(60) # รอให้เซิร์ฟเวอร์หลักรันขึ้นมาก่อนค่อยเริ่ม Ping

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # เติม https:// หากไม่มี
                target = f"https://{url}" if not url.startswith("http") else url
                async with session.get(target, timeout=15) as resp:
                    if resp.status == 200:
                        logger.info(f"🏓 Self-ping OK (Status: {resp.status}) - Bot stays alive!")
                    else:
                        logger.warning(f"⚠️ Self-ping Warning: Code {resp.status}")
            except Exception as e:
                logger.error(f"❌ Self-ping Error: {e}")

            await asyncio.sleep(300) # สะกิดทุกๆ 5 นาที (300 วินาที)

async def cipher_bridge_server(center: Any):
    """🧠 CIPHER BRIDGE API - เปิด HTTP Server ให้ Cipher AI สั่งงานบอทได้โดยตรง"""
    
    async def get_status(request):  # pyre-ignore
        acc = await center._get_cached_account()
        usdt = next((a for a in acc['assets'] if a['asset'] == 'USDT'), None) if acc else None  # pyre-ignore
        w_bal = safe_float(usdt['walletBalance']) if usdt else 0.0  # pyre-ignore
        pos = next((p for p in acc['positions'] if p['symbol'] == center.symbol), None) if acc else None  # pyre-ignore
        p_amt = safe_float(pos['positionAmt']) if pos else 0.0  # pyre-ignore
        entry_p = safe_float(pos['entryPrice']) if pos else 0.0  # pyre-ignore
        pnl = safe_float(pos['unrealizedProfit']) if pos else 0.0  # pyre-ignore
        return web.json_response({
            "status": "paused" if center.gl_paused else "running",
            "mode": center.strategy_mode,
            "symbol": center.symbol,
            "balance_usdt": w_bal,
            "price": center.current_price,
            "position_btc": p_amt,
            "entry_price": entry_p,
            "pnl_usdt": pnl,
            "layers": center.active_layers,
            "next_buy": center.next_buy_price,
            "grid_step_pct": center.grid_step_pct,
            "vol_24h": getattr(center, 'vol_24h', 0),
            "inst_vol": getattr(center, 'inst_vol_val', 0)
        })

    async def cmd_handler(request):  # pyre-ignore
        try:
            data = await request.json()
            cmd = data.get("cmd", "")
            result = ""
            if cmd == "pause":
                await center.set_pause(True); result = "Paused ✅"
            elif cmd == "resume":
                await center.set_pause(False); result = "Resumed ✅"
            elif cmd == "mode_safe":
                center.strategy_mode = "SAFE"
                await center.update_strategy_parameters()
                result = "Mode: SAFE 🛡️"
            elif cmd == "mode_profit":
                center.strategy_mode = "PROFIT"
                await center.update_strategy_parameters()
                result = "Mode: PROFIT 💸"
            elif cmd == "mode_normal":
                center.strategy_mode = "NORMAL"
                await center.update_strategy_parameters()
                result = "Mode: NORMAL 🔄"
            elif cmd == "report":
                await center.send_combined_report(); result = "Report Sent 📊"
            elif cmd == "close_all":
                await center.emergency_close(); result = "Emergency Close Triggered 💥"
            else:
                return web.json_response({"error": f"Unknown cmd: {cmd}"}, status=400)
            
            logger.info(f"🧠 Cipher Bridge CMD: {cmd} -> {result}")
            return web.json_response({"ok": True, "result": result})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    app = web.Application()
    app.router.add_get('/cipher/status', get_status)
    app.router.add_post('/cipher/cmd', cmd_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 3001)
    await site.start()
    logger.info("🧠 Cipher Bridge API running on http://127.0.0.1:3001")
    logger.info("   GET  /cipher/status  - ดูสถานะ")
    logger.info("   POST /cipher/cmd     - สั่ง: pause/resume/mode_safe/mode_profit/mode_normal/report/close_all")
    while True:
        await asyncio.sleep(3600)

async def main():
    center = MainCommandCenter()
    # ตรวจสอบว่ามี EXTERNAL_URL หรือไม่ (ถ้ามีคือรันบน Cloud)
    external_url = os.environ.get('EXTERNAL_URL')

    tasks = [center.run()]

    if external_url:
        # ☁️ โหมด Webhook (Cloud)
        await health_server(center.tg)
        await center.tg.set_webhook(external_url)
        tasks.append(self_ping())
    else:
        # 🏠 โหมด Local / คอมบ้าน (ใช้ Polling แทน Webhook)
        logger.info("🏠 EXTERNAL_URL not set. Running in LOCAL MODE with Telegram Polling.")
        tasks.append(center.tg.poll_commands())  # เปิด Polling รับคำสั่ง Telegram
        tasks.append(cipher_bridge_server(center)) # 🧠 เปิด Cipher AI Bridge API
    
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
