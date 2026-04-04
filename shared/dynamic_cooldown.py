import numpy as np
import time
from collections import deque
from enum import Enum

class MarketRegime(Enum):
    CHOPPY = "CHOPPY"
    TRENDING = "TRENDING"
    UNKNOWN = "UNKNOWN"

class CooldownState(Enum):
    READY = "READY"
    PAUSED = "PAUSED"      # หยุดชั่วคราวในสภาวะ Choppy
    BLOCKED = "BLOCKED"    # บล็อกในสภาวะ Trending
    RECOVERY = "RECOVERY"  # รอการฟื้นตัวกลับสู่ Choppy แบบเต็มรอบ

class DynamicCooldownManager:
    def __init__(self, 
                 window_size=30,           # ล็อกขนาด Window สำหรับหาค่าเฉลี่ย
                 choppy_threshold=2.5,     # ratio (sigma/drift) > 2.5 ถือเป็น Choppy
                 n_safe_bars=3,            # จำนวนแท่ง (หรือ Tick) ที่ปลอดภัยก่อนเริ่มเทรดใหม่ใน Choppy
                 ofi_threshold=0.5,        # Threshold ของความผิดปกติของ Order Flow
                 cusum_k=1.5,              # CUSUM drift allowance
                 cusum_h=5.0,              # CUSUM threshold เปลี่ยนผ่านสู่ Trending
                 full_cooldown_sec=300,    # ระยะเวลา Full Cooldown เมื่อเกิด Trend (วินาที)
                 max_trades_per_min=5      # Throttle: ขีดจำกัดจำนวนไม้ที่เปิดได้ใน 1 นาที
                 ):
        
        # State & Regimes
        self.state = CooldownState.READY
        self.regime = MarketRegime.UNKNOWN
        
        # Parameters
        self.window_size = window_size
        self.choppy_threshold = choppy_threshold
        self.n_safe_bars = n_safe_bars
        self.ofi_threshold = ofi_threshold
        self.cusum_k = cusum_k
        self.cusum_h = cusum_h
        self.full_cooldown_sec = full_cooldown_sec
        self.max_trades_per_min = max_trades_per_min
        
        # Memory / Deques
        self.prices = deque(maxlen=window_size)
        
        # Counters & Timers
        self.safe_bar_count = 0
        self.block_timestamp = 0
        self.trade_timestamps = deque()
        
        # CUSUM vars
        self.s_pos = 0.0
        self.s_neg = 0.0
        
        # OFI vars
        self.prev_bid_price = 0
        self.prev_bid_qty = 0
        self.prev_ask_price = 0
        self.prev_ask_qty = 0

    def update_market_data(self, price, bid_price=0, bid_qty=0, ask_price=0, ask_qty=0):
        """รับข้อมูลล่าสุดเพื่อคำนวณสภาวะตลาด (Regime) และ CUSUM"""
        self.prices.append(price)
        
        if len(self.prices) < 2:
            return  # ข้อมูลไม่พอ
        
        # 1. คำนวณ OFI (Order Flow Imbalance) หากมีข้อมูล Orderbook
        ofi = self._calculate_ofi(bid_price, bid_qty, ask_price, ask_qty)
        
        # 2. คำนวณ Regime (Volatility vs Drift)
        # ใช้ np.diff เพื่อหาการเปลี่ยนแปลง
        price_diffs = np.diff(list(self.prices))
        drift = np.mean(price_diffs)
        volatility = np.std(price_diffs)
        
        if abs(drift) > 1e-8:
            ratio = volatility / abs(drift)
        else:
            ratio = float('inf')  # ไม่มี Drift (Sideways สมบูรณ์) ถือเป็น Choppy
            
        old_regime = self.regime
        if ratio > self.choppy_threshold:
            self.regime = MarketRegime.CHOPPY
        else:
            self.regime = MarketRegime.TRENDING
            
        # 3. คำนวณ CUSUM Breakout (สมมติให้ target mean คือ 0 ของการเปลี่ยนแปลง)
        current_diff = price_diffs[-1]
        std_diff = volatility if volatility > 1e-8 else 1.0 # normalize
        z = current_diff / std_diff 
        
        self.s_pos = max(0, self.s_pos + z - self.cusum_k)
        self.s_neg = max(0, self.s_neg - z - self.cusum_k)
        
        cusum_triggered = (self.s_pos > self.cusum_h) or (self.s_neg > self.cusum_h)

        # 4. State Machine Logic (อัปเดตสถานะบอท)
        self._update_state_machine(ofi, cusum_triggered)

    def _calculate_ofi(self, bid_price, bid_qty, ask_price, ask_qty):
        """คำนวณ Order Flow Imbalance อย่างง่าย เพื่อใช้เป็น Filter ในสภาวะ Choppy"""
        if self.prev_bid_price == 0:
            self.prev_bid_price, self.prev_bid_qty = bid_price, bid_qty
            self.prev_ask_price, self.prev_ask_qty = ask_price, ask_qty
            return 0
            
        # Bid Delta
        if bid_price > self.prev_bid_price:
            e_bid = bid_qty
        elif bid_price == self.prev_bid_price:
            e_bid = bid_qty - self.prev_bid_qty
        else:
            e_bid = -self.prev_bid_qty
            
        # Ask Delta
        if ask_price < self.prev_ask_price:
            e_ask = ask_qty
        elif ask_price == self.prev_ask_price:
            e_ask = ask_qty - self.prev_ask_qty
        else:
            e_ask = -self.prev_ask_qty
            
        self.prev_bid_price, self.prev_bid_qty = bid_price, bid_qty
        self.prev_ask_price, self.prev_ask_qty = ask_price, ask_qty
        
        return e_bid - e_ask

    def _update_state_machine(self, ofi, cusum_triggered):
        current_time = time.time()
        
        # กรณีTrending / CUSUM แตก (Breakout)
        if cusum_triggered or self.regime == MarketRegime.TRENDING:
            if self.state != CooldownState.BLOCKED:
                self.state = CooldownState.BLOCKED
                self.block_timestamp = current_time
                self.s_pos = 0.0  # Reset CUSUM
                self.s_neg = 0.0
                print(f"[{current_time}] ⚠️ CUSUM Breakout! เปลี่ยนสภาวะเป็น TRENDING บล็อกบอท...")
            return

        # การฟื้นตัวจาก Blocked (Full Cooldown Recovery)
        if self.state == CooldownState.BLOCKED:
            if self.regime == MarketRegime.CHOPPY:
                if (current_time - self.block_timestamp) > self.full_cooldown_sec:
                    self.state = CooldownState.RECOVERY
                    self.safe_bar_count = 0
                    print(f"[{current_time}] 🔄 ผ่านช่วง Full Cooldown แล้ว เริ่มกระบวนการ Recovery...")
            return

        # การประเมินในสภาวะ Choppy Market (Normal & Recovery)
        if self.regime == MarketRegime.CHOPPY and self.state in [CooldownState.RECOVERY, CooldownState.PAUSED, CooldownState.READY]:
            # กรอง Noise ด้วย OFI
            if abs(ofi) > self.ofi_threshold:
                self.safe_bar_count += 1
            else:
                self.safe_bar_count = 0 # โดน Noise รีเซ็ตจำนวนแท่ง
                self.state = CooldownState.PAUSED
                
            if self.safe_bar_count >= self.n_safe_bars:
                if self.state != CooldownState.READY:
                    print(f"[{current_time}] ✅ สภาวะ CHOPPY เสถียรผ่านเกณฑ์ Re-arming เป็น Ready!")
                self.state = CooldownState.READY
            else:
                self.state = CooldownState.PAUSED

    def record_trade(self):
        """บันทึกเวลาเปิดไม้เพื่อนำไปเช็ค Throttling"""
        self.trade_timestamps.append(time.time())

    def is_safe_to_trade(self) -> bool:
        """ตรวจสอบว่าสามารถยิงออเดอร์ (เปิดไม้) ได้หรือไม่"""
        current_time = time.time()
        
        # 1. เช็ค Regime State (ต้อง READY)
        if self.state != CooldownState.READY:
            return False
            
        # 2. เช็ค Throttling (Spam Filter)
        while self.trade_timestamps and (current_time - self.trade_timestamps[0] > 60):
            self.trade_timestamps.popleft() # เอาประวัติที่เกิน 1 นาทีออก
            
        if len(self.trade_timestamps) >= self.max_trades_per_min:
            print(f"[{current_time}] 🚫 Throttling Triggered! ยิงรัวเกินไป {len(self.trade_timestamps)} ไม้ใน 1 นาที")
            self.state = CooldownState.PAUSED
            self.safe_bar_count = 0 
            return False
            
        return True

    def get_status(self):
        return {
            "state": self.state.value,
            "regime": self.regime.value,
            "safe_bar_count": self.safe_bar_count,
            "cusum": {"pos": round(self.s_pos, 2), "neg": round(self.s_neg, 2)},
            "throttling": len(self.trade_timestamps)
        }
