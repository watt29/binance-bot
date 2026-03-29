# 🏰 COMMANDER v2.0: INSTITUTIONAL-GRADE MICROSTRUCTURE BOT
ระบบบอทเทรด Binance Futures ที่ออกแบบตามหลัก **Market Microstructure** ระดับสถาบัน ผสาน Order Book Analysis, Whale Signal, Spoof Detection, Trade-based OBI, Regime Detection, Dynamic Kill Switch, VolatilityGate, Dynamic Lot Sizing, Equity Kill Switch, Variance Age Exit, Flip Logger, Auto-Monitor และ Cloudflare Worker IP Bypass ไว้ในไฟล์เดียว (`main_commander.py`)

**Philosophy: "รอดให้ได้ก่อน แล้วค่อยทำกำไร"**

---

## 🏗️ 1. สถาปัตยกรรมระบบ (System Architecture)

ทำงานด้วย **asyncio** + **WebSocket** แบบ Event-driven เต็มรูปแบบ ไม่ block thread:

| Component | หน้าที่ |
|---|---|
| **Trading Engine** | DCA Grid ซื้อ/ขาย, Trailing Stop, OBI+OFI Filter |
| **Order Book Engine** | Local Order Book Sync (Event-based), OBI, OFI, Spoof Detector (3-Gate) |
| **Trade OBI Engine** | aggTrade WebSocket → OBI^T Rolling 30s Window (O(1)) |
| **Regime Detector** | วิเคราะห์ CHOPPY/TRENDING/VOLATILE จาก price_buffer |
| **VolatilityGate** | σ/μ Ratio (Volatility/Drift) ตรวจ Trending แบบ Real-time |
| **Flip Logger** | บันทึกทุก OBI Flip + ตรวจ outcome 5 นาทีทีหลัง (Self-Learning) |
| **Whale Signal** | Tiered Wall Classification (WATCH/STRONG/MEGA) |
| **OBI Flip Alert** | Triple-Verified Early Warning (4 Gates) ตรวจจับ Liquidity Vacuum |
| **Kill Switch** | Dynamic Cooldown ตาม Regime (CHOPPY 60s / TRENDING 300s / VOLATILE 240s) |
| **Equity Kill Switch** | หยุด DCA + ยกเลิก Orders เมื่อ Drawdown > 30% |
| **Dynamic Lot Sizing** | ลด Lot อัตโนมัติตาม Layer (100% → 30%) — Downward Protection |
| **Variance Age Exit** | บังคับปิด Position เมื่อถือเกิน 72 ชั่วโมง |
| **Auto-Monitor** | ตรวจพอร์ต 6 rules อัตโนมัติทุก 60 วินาที |
| **Telegram Interface** | รับคำสั่ง + ส่ง Dashboard ผ่าน Outbound Token Bucket |
| **SAE (Survivability)** | บริหาร API Weight ป้องกัน IP Ban |
| **Cloudflare Worker Proxy** | มุด IP ผ่าน CF edge เมื่อ VPS ถูก Binance block (100k req/day ฟรี) |

---

## 🧱 2. 12-Layer Institutional Architecture

ระบบแบ่งเป็น 4 เสาหลักตามหลัก Quantitative Trading:

### เสา 1: Signal Generation
| Layer | ชื่อ | หน้าที่ |
|---|---|---|
| L1 | **OBI** | วัดความไม่สมดุล Order Book (Limit Order snapshot pressure) |
| L2 | **OFI** | วัด Real Flow จริง (sign-based, 30-tick smooth buffer) |
| L3 | **Trade OBI (OBI^T)** | วัดแรงซื้อ/ขายจาก Executed Trades จริง (aggTrade 30s rolling) |

### เสา 2: Manipulation Filter
| Layer | ชื่อ | หน้าที่ |
|---|---|---|
| L4 | **3-Gate Spoof** | กรองกำแพงหลอก (Volume + Distance + Time) |
| L5 | **Tiered Wall** | จัดระดับความน่าเชื่อถือ (WATCH/STRONG/MEGA) |

### เสา 3: Market Context & Learning
| Layer | ชื่อ | หน้าที่ |
|---|---|---|
| L6 | **Regime Detector** | ระบุสภาวะตลาด CHOPPY/TRENDING/VOLATILE จาก price range |
| L7 | **VolatilityGate** | σ/μ Ratio ตรวจ Trending แบบ Real-time + Re-arm 3 bars |
| L8 | **Flip Logger** | บันทึก OBI Flip events + outcome เพื่อ self-learning pattern |

### เสา 4: Risk Management & Survival
| Layer | ชื่อ | หน้าที่ |
|---|---|---|
| L9 | **OBI Flip Alert** | Early Warning (4 Gates) + Dynamic Kill Switch ตาม Regime |
| L10 | **Equity Kill Switch** | Drawdown > 30% → หยุด DCA + Cancel All Orders |
| L11 | **Dynamic Lot Sizing** | ลด Lot ตาม Layer — Downward Protection |
| L12 | **Auto-Monitor** | ตรวจพอร์ต 6 rules + Variance Age Exit 72 ชั่วโมง |

---

## 🧠 3. กลยุทธ์การเทรด (Core Strategy)

### 3.1 Ultra-Adaptive Grid (DCA สูงสุด 12 ไม้)
- **Grid Step** คำนวณจากความผันผวน 24 ชม. (0.5%–2.0%) และ Instantaneous Volatility จาก price_buffer
- ถ้า price_buffer ยังไม่พอ (< 2 entries หลัง restart): ใช้ `vol_24h / 24` เป็น proxy inst_vol แทน
- ตั้งแต่ไม้ที่ 4+ ระยะห่างขยาย ×1.25, ×1.50... (Deep DCA Guard)
- Auto-recover mode จาก Layer count เมื่อ restart (ไม่ reset เป็น NORMAL)

**สูตรคำนวณ Grid Step:**
```
base_step   = max(0.5, min(2.0, vol_24h / 8))
inst_vol    = (max(price_buffer) - min(price_buffer)) / min(price_buffer) × 100
grid_step   = base_step + (inst_vol × 0.8)

SAFE mode (Layer < 10):   grid_step × 1.5   (+50%)
SAFE mode (Layer ≥ 10):   grid_step × 3.0   (+200%) ← CRITICAL SAFE
```

### 3.2 Dynamic Grid Step Scaling ตาม Layer
เมื่อบอทเข้า SAFE Mode ระยะ Grid จะถูกคูณตาม Layer เพื่อรอ Mean-reversion ที่ไกลกว่า:

| Layer | Mode | Grid Multiplier | เหตุผล |
|---|---|---|---|
| 1–5 | NORMAL | ×1.0 (100%) | เทรดปกติ |
| 6–7 | PRE-SAFE | ×1.25 (125%) | เริ่มระวัง |
| 8–9 | SAFE | ×1.5 (150%) | ถอยระยะ 50% |
| 10–12 | CRITICAL SAFE | ×3.0 (300%) | ป้องกัน Margin แตก |

### 3.3 Trailing Stop (Full Close)
- เปิด Trailing เมื่อราคาถึง Take Profit target
- ปิด Position ทั้งหมดเมื่อราคาหลุด Peak × 0.9995
- **OBI Boost**: ถ้า OBI < -0.6 → ปิดเร็วขึ้น (Peak × 0.9998)

### 3.4 Strategy Modes
| Mode | เงื่อนไข | พฤติกรรม |
|---|---|---|
| **NORMAL** | Layer < 6, ไม่มีวิกฤต | เทรดปกติ Grid Step เต็ม |
| **SAFE** | Layer ≥ 6 (auto) หรือสั่งมือ | ถอยระยะ 50–200%, ลด TP |
| **PROFIT** | กำไร ≥ $2, Layer < 6 | ลด TP 50% ปิดงานเร็ว |

---

## 📐 4. Dynamic Lot Sizing (Downward Protection)

ลดขนาด Lot อัตโนมัติเมื่อ Layer ลึกขึ้น เพื่อชะลอ Triangular Loss Growth:

| Layer | Lot Scale | ผลลัพธ์ |
|---|---|---|
| 1–5 | **100%** | Lot ปกติ |
| 6–7 | **75%** | ลด 25% |
| 8–9 | **50%** | ลด 50% |
| 10+ | **30%** | ลด 70% — รักษา Margin ก้อนสุดท้าย |

**สูตร:**
```
base_lot  = max(200, min((wallet_balance × 0.8 / 5) × leverage, 500))
lot_scale = _get_lot_scale()     # ตาม Layer ปัจจุบัน
actual_lot = base_lot × lot_scale
```

**ทำไมไม่ใช้ Martingale:**
- Martingale: Lot เพิ่มแบบ Exponential → พอร์ตแตกเร็วมาก
- Downward Lot: Lot ลดแบบ Fractional → ยืดอายุพอร์ต + รอ Mean-reversion ได้นานกว่า

---

## 🌡️ 5. VolatilityGate (σ/μ Ratio Real-time)

ตรวจจับ Regime แบบ Real-time ด้วยอัตราส่วน Volatility ต่อ Drift:

### หลักการ
```
σ = std dev ของ log returns (ความผันผวน)
μ = mean absolute MA change (ทิศทาง Drift)

σ/μ Ratio สูง  → CHOPPY  (Volatility ครอบงำ)  → ปลอดภัยสำหรับ Grid
σ/μ Ratio ต่ำ  → TRENDING (Drift ครอบงำ)     → อันตราย บล็อก DCA
```

### เกณฑ์
| สภาวะ | σ/μ Ratio | Action |
|---|---|---|
| **CHOPPY (ปลอดภัย)** | ≥ 5.0 | Grid ทำงานปกติ |
| **TRENDING (อันตราย)** | < 5.0 | บล็อก DCA ทันที |
| **Ranging สมบูรณ์** | ≈ 999 (μ → 0) | CHOPPY สูงสุด |

### Re-arming (ป้องกัน Pause/Resume Oscillation)
บอทจะไม่ Re-arm ทันทีที่ σ/μ เด้งกลับ ต้องผ่านเงื่อนไข:
```
σ/μ ≥ 5.0 ติดต่อกัน 3 รอบ (VGATE_REARM_BARS = 3) → Re-arm
```
ป้องกันบอทเปิดๆ ปิดๆ จากสัญญาณ Whipsaw

---

## 🐳 6. Whale Signal System (Order Book Analysis)

### 6.1 OBI — Order Book Imbalance (Limit Order-based, Flat Weight)
```
OBI_flat = (Σ Bid_price × Bid_qty  -  Σ Ask_price × Ask_qty)
           ─────────────────────────────────────────────────
           (Σ Bid_price × Bid_qty  +  Σ Ask_price × Ask_qty)

source: Top 20 levels, น้ำหนักเท่ากันทุก level
```

ช่วงค่า -1.0 ถึง +1.0:

| ค่า OBI | สัญญาณ | ความหมาย |
|---|---|---|
| +0.65 ถึง +1.0 | 🐳 STRONG BUY | Whale กดซื้อหนักมาก |
| +0.3 ถึง +0.65 | 🟢 Buy Pressure | แรงซื้อสุทธิ |
| -0.3 ถึง +0.3 | ⚖️ Balanced | ตลาดสมดุล |
| -0.65 ถึง -0.3 | 🔴 Sell Pressure | แรงขายสุทธิ |
| -1.0 ถึง -0.65 | 🐳 STRONG SELL | Whale ทุบขาย |

**ข้อจำกัด**: OBI เป็น L2 Data (Aggregated) — Noise สูง, Decay ใน ~5 วินาที ต้องใช้ OFI และ Trade OBI ยืนยันก่อน confirmed

### 6.2 OBI Deep — Distance-Weighted OBI (Top 5 Levels)
วัด Order Book Imbalance แบบถ่วงน้ำหนักตามระยะห่างจาก mid-price — ใช้เฉพาะ **Top 5 levels**:

```
w_i     = 1 / (1 + d_i)
d_i     = |price_i - mid_price| / tick_size   (tick_size = 0.10)

OBI_deep = (Σ w_i × Bid_qty  -  Σ w_i × Ask_qty)
           ────────────────────────────────────────
           (Σ w_i × Bid_qty  +  Σ w_i × Ask_qty)
```

**ตัวอย่าง weight decay (BTC/USDT, mid = $100.00):**
| Level | ห่างจาก mid | Tick distance | Weight |
|---|---|---|---|
| L1 | $0.10 | 1 tick | 0.500 |
| L2 | $0.20 | 2 ticks | 0.333 |
| L3 | $0.30 | 3 ticks | 0.250 |
| L4 | $0.40 | 4 ticks | 0.200 |
| L5 | $0.50 | 5 ticks | 0.167 |

**Spoof Detection จาก OBI_flat vs OBI_deep:**
```
OBI_flat สูง + OBI_deep ต่ำ → กำแพงอยู่ไกล mid → สัญญาณ Layering/Spoofing
gap = OBI_flat - OBI_deep > 0.25 → แสดง 🔍(Deep diverge — wall ไกล mid) ใน Dashboard
```

### 6.3 OFI — Order Flow Imbalance (Stabilizer)
วัดการเปลี่ยนแปลง best bid/ask ทุก tick แบบ sign-based:
```
bid ขึ้น หรือ qty เพิ่ม  →  +1  (buy flow)
ask ลด  หรือ qty ลด     →  +1  (buy flow)
ตรงข้าม                 →  -1  (sell flow)
```
- Smooth buffer 30 ticks → normalize -1.0 ถึง +1.0
- บทบาท: **Anchor Regime** — ตรึงสภาวะตลาด ไม่ react ตาม Noise ทุก tick
- OBI + OFI ชี้ทิศเดียวกัน = "confirmed" → บอทอนุญาตเปิดไม้
- OBI สูง + OFI ติดลบ = "Trades Oppose Quotes" = สัญญาณ Spoofing ⚠️

### 6.4 Trade OBI — OBI^T (Executed Trade-based)
วัดแรงซื้อ/ขายจาก **Executed Trades จริง** ผ่าน aggTrade WebSocket (ทุก 100ms):
```
source:  wss://fstream.binance.com/ws/btcusdt@aggTrade
field m: is_buyer_maker
         True  → Taker ขาย (Market Sell)
         False → Taker ซื้อ (Market Buy)

OBI^T = (V_buy - V_sell) / (V_buy + V_sell)
        rolling window = 30 วินาที (O(1) deque)
```

| เปรียบเทียบ | OBI_flat | OBI_deep | Trade OBI (OBI^T) |
|---|---|---|---|
| แหล่งข้อมูล | L2 snapshot Top 20 | L2 snapshot Top 5 | Executed trades จริง |
| Weighting | flat (p×q) | distance-weighted (tick) | volume-based rolling |
| ถูก Spoof ได้ไหม | ใช่ | น้อยกว่า (level 6–20 ถูกตัด) | ไม่ได้ |
| Decay | ~5 วินาที | ~5 วินาที | Rolling 30 วินาที |
| บทบาทหลัก | Entry/DCA gate | Secondary confirm + Spoof hint | Ultimate Gate (OBI Flip Gate 4) |

### 6.5 3-Gate Spoof Detector
| Gate | เงื่อนไข | หลักการ |
|---|---|---|
| **Gate 1** | Volume ≥ 6× avg | กำแพงจริงต้องหนักกว่าสภาพคล่องปกติ |
| **Gate 2** | ห่างจาก mid-price ≤ 0.5% | ไกลกว่านี้ = Bait Wall ล่อรายย่อย |
| **Gate 3** | อยู่นาน ≥ 3 วินาที | หายเร็ว = Spoof (Quote Life Span สั้น) |

### 6.6 Tiered Wall Classification
| Icon | Tier | Threshold | ความหมาย |
|---|---|---|---|
| 🟡 | WATCH | ≥ 6× avg | Retail Whale |
| 🟠 | STRONG | ≥ 8× avg | Institutional |
| 🔴 | MEGA | ≥ 15× avg | S-class / OTC level |

### 6.7 OBI Flip Alert — Triple Verified (4 Gates)
ตรวจจับ Liquidity Vacuum ด้วย 4 ด่าน:

| Gate | เงื่อนไข | หลักการ |
|---|---|---|
| **Gate 1** | `OBI_SMA(5) < -0.15` | OBI ร่วงต่อเนื่อง (ไม่ใช่แค่ 1 tick) |
| **Gate 2** | `OBI_recent_max(30) ≥ +0.60` | เคยสูงมาก้อน (พลิกตัวจริง) |
| **Gate 3** | `OFI < -0.20` | Best bid/ask delta ยืนยัน |
| **Gate 4** | `Trade OBI < -0.15` | Executed trades ยืนยัน Market Sell จริง |

**ตัวอย่าง Alert:**
```
🚨 OBI FLIP ALERT (Triple Verified)!
OBI ร่วงจาก +0.92 → -0.57
📉 OFI: -0.25 | Trade OBI: -0.42
🧭 Regime: CHOPPY → ชะลอระบบ 60s (แรงขายจริง ไม่ใช่ Spoof)
```

**Diagnostic Log (เมื่อ Gate 4 Block):**
```
🟡 OBI FLIP BLOCKED by TradeOBI: +0.09 (Regime CHOPPY 0.02%) — Flickering Liquidity ignored
```

---

## 🧭 7. Regime Detector

ตรวจจับสภาวะตลาดจาก price_buffer — ใช้งานจริงในการปรับ Dynamic Cooldown ของ Kill Switch:

```python
rng = (max(price_buffer) - min(price_buffer)) / min(price_buffer) × 100
```

| Regime | เงื่อนไข | Icon | KS Cooldown | ความหมาย |
|---|---|---|---|---|
| **CHOPPY** | rng < 0.15% | ↔️ | 60s | Sideways ผันผวนน้อย Liquidity กลับเร็ว |
| **TRENDING** | 0.15% ≤ rng ≤ 0.8% | 📈 | 300s | มีทิศทาง รอ Momentum หมดแรงก่อน |
| **VOLATILE** | rng > 0.8% | ⚡ | 240s | ผันผวนสูง Capital Preservation สูงสุด |
| **WARMING** | buffer < 2 entries | 🌡️ | 60s | เพิ่งเริ่ม รอข้อมูล (~10s) |
| **ERROR** | Exception | ⚠️ | 60s | ข้อผิดพลาดภายใน |

> **หมายเหตุ**: TRENDING Cooldown เพิ่มจาก 120s → **300s** เพื่อรอให้ Momentum หมดแรงจริงๆ ก่อน Re-arm

---

## 📓 8. Flip Logger — Self-Learning Pattern Engine

บันทึกทุก OBI Flip event อัตโนมัติ และตรวจ outcome หลัง 5 นาที:

**เก็บอะไร (ต่อ 1 event):**
```
time, obi_before, obi_after, trade_obi, ofi,
price, regime, cooldown
→ outcome_price, outcome_delta (%), outcome_label
```

**Labels:**
| Label | เงื่อนไข | ความหมาย |
|---|---|---|
| 🟢 BOUNCE | delta ≥ +0.1% | ราคาขึ้นหลัง Flip = Kill Switch ช่วยได้ |
| 🔴 DUMP | delta ≤ -0.1% | ราคาลงต่อ = ห้ามเทรดถูกต้อง |
| ⚪ FLAT | -0.1% < delta < +0.1% | ราคาทรงตัว |

**Insight จาก Data จริง (n=25):**
```
CHOPPY  → BOUNCE 8% | DUMP 0% | FLAT 92%   ← OBI Flip ใน CHOPPY = Noise ล้วนๆ
TRENDING → BOUNCE 0% | DUMP 100% | FLAT 0%  ← สัญญาณจริง (แต่ sample น้อย)
```

**ตัวอย่าง Flip Stats (กด `📓 Flip Stats` ใน Telegram):**
```
📓 Flip Log Stats (25 events)
  CHOPPY  → 🟢 BOUNCE 8% | 🔴 DUMP 0% | ⚪ FLAT 92% (n=24)
  TRENDING → 🟢 BOUNCE 0% | 🔴 DUMP 100% | ⚪ FLAT 0% (n=1)
─ ล่าสุด 3 events ─
  03:26 CHOPPY FLAT -0.002% | OBI +0.98→-0.59 T:-0.34
  03:34 CHOPPY FLAT +0.043% | OBI +0.97→-0.73 T:-0.85
  04:09 CHOPPY FLAT +0.008% | OBI +0.72→-0.83 T:-0.39
```

---

## 🤖 9. Auto-Monitor (6 Rules)

ตรวจพอร์ตอัตโนมัติทุก 60 วินาที:

| Rule | เงื่อนไข | Action |
|---|---|---|
| 1 | กำไร ≥ $5 | ปิด Position อัตโนมัติทันที |
| 2 | กำไร ≥ $2 + Layer < 6 | เปลี่ยนเป็น PROFIT mode |
| 3 | ขาดทุน ≤ -$120 | แจ้งเตือน Telegram ฉุกเฉิน |
| 4 | Layer ≥ 6 | บังคับ SAFE mode |
| 5 | Layer ≥ 8 (CRITICAL) | บังคับ SAFE + Grid ×3.0 (CRITICAL SAFE) |
| 6 | Position ถือ ≥ 72 ชั่วโมง | บังคับปิด (Variance Age Exit) |

---

## 🛡️ 10. ระบบความปลอดภัย (Safety Systems)

### 10.1 Dynamic Kill Switch (Regime-Aware)
**Trigger 1 — Volatility Spike:**
- inst_vol > 1.0% สะสม ≥ 2 ครั้งติดต่อกัน → Kill Switch ON
- Cooldown: Dynamic ตาม Regime

**Trigger 2 — OBI Flip (4 Gates):**
- ผ่านครบ 4 Gates → Kill Switch ON พร้อมแจ้ง Telegram
- **CHOPPY: Bypass** (92% FLAT empirical data — Mechanical Noise)
- **TRENDING/VOLATILE: Active** (Informational Signal)

**Dynamic Cooldown (ปรับอัตโนมัติตาม Regime จริง ณ ขณะนั้น):**
| Regime | Icon | Cooldown | เหตุผล |
|---|---|---|---|
| **CHOPPY** | ↔️ | 60s | Liquidity กลับเร็ว ไม่เสียโอกาส |
| **TRENDING** | 📈 | **300s** | รอ Momentum หมดแรงก่อน (เพิ่มจาก 120s) |
| **VOLATILE** | ⚡ | 240s | Capital Preservation สูงสุด |
| **WARMING / ERROR** | 🌡️⚠️ | 60s | ค่าปลอดภัย รอข้อมูลครบ |

### 10.2 Equity Kill Switch (Drawdown-based)
หยุด DCA และยกเลิก Open Orders ทั้งหมดเมื่อพอร์ตวิกฤต:

```
Drawdown = PNL / (Available Balance + |PNL|)
Drawdown ≤ -30% → Equity Kill Active
  1. ยกเลิก Open Orders ทั้งหมด (คืน Margin)
  2. บล็อก DCA ทุก Layer
  3. แจ้งเตือน Telegram
  4. รอ User Reset ด้วยคำสั่ง 🔄 NORMAL หรือ ▶️ รันต่อ
```

**Emergency Balance Lock (เพิ่มเติม):**
```
Available Balance < $30 → บล็อก DCA ทันที (รักษา Margin ไว้)
แจ้งเตือนทุก 5 นาที (ไม่ spam)
```

### 10.3 VolatilityGate (σ/μ Filter)
บล็อก DCA เมื่อ σ/μ < 5.0 (Trending detected) — ดูหัวข้อ 5 สำหรับรายละเอียด

### 10.4 Variance Age Exit (Time-based Stop)
```
Position ถือเกิน 72 ชั่วโมง → บังคับปิดอัตโนมัติ
เหตุผล: Variance ของพอร์ต Grid เติบโตแบบ Exponential ตามเวลา
         ยิ่งถือนาน → โอกาสเจอเหตุการณ์ล้างพอร์ตสูงขึ้น
```

### 10.5 Outbound Token Bucket (Telegram Rate Limiter)
ป้องกัน Telegram API Error 429:
```
Capacity:    3 tokens  (burst สูงสุด 3 ข้อความ)
Refill Rate: 1 token/วินาที
Queue:       สูงสุด 10 ข้อความ
```

### 10.6 Local Order Book — Event-based Sync
แก้ปัญหา Windows latency (~17,578 update IDs/sec gap):
```
1. รับ WebSocket depth event ตรงๆ ไม่ต้อง REST snapshot
2. Accumulate 10 events → _lob_ready = True
3. gap เล็ก (< 50k) → ข้ามไปต่อ
4. gap ใหญ่ (≥ 50k) → reset และ sync ใหม่
```

### 10.7 SAE (Survivability-Aware Execution)
| Weight Usage | Action |
|---|---|
| > 60% | throttle low-priority (delay 2s) |
| > 80% | throttle non-critical (delay 3s) |
| > 95% | block ทุกคำสั่งยกเว้น trade |
| 429/418 | Backoff อัตโนมัติ ไม่ crash |

### 10.8 Anti-Falling Knife
- ไม่เปิดไม้แรกเมื่อราคา > 85% ของกรอบ 24 ชม.
- ไม่เปิดไม้แรกเมื่อ Instant Volatility > 0.4%
- ไม่เปิดไม้แรกเมื่อ VolatilityGate = BLOCKED

### 10.9 Max Daily Loss (Prop Firm Style)
หยุดเทรดทั้งวันเมื่อขาดทุนสะสม (Realized) เกินเกณฑ์:
```
MAX_DAILY_LOSS = -$50/วัน
Realized PnL สะสม ≤ -$50 → _daily_kill_active = True
  - บล็อกทั้งไม้แรกและ DCA ตลอดวัน
  - Reset อัตโนมัติเมื่อขึ้นวันใหม่ (UTC 00:00)
  - แจ้งเตือน Telegram ทันทีที่ trigger
```

### 10.10 Rapid-Fire Order Throttle
ป้องกันบอทส่ง order รัวผิดปกติ (Algorithm Malfunction / Runaway Bot):
```
สูงสุด 3 orders ใน 60 วินาที (sliding window)
เกินเกณฑ์ → บล็อก + แจ้งเตือน Telegram ทันที
หลุดจาก window อัตโนมัติ → ทำงานต่อได้เอง
```

### 10.11 Session Recovery
- Restart → ตรวจ Layer count จาก Position จริง
- Layer ≥ 6 → auto-set SAFE mode ทันที (ไม่ reset)

---

## 📟 11. Telegram Menu

```
┌─────────────────────────────────┐
│  📊 พอร์ต   💰 กำไรวันนี้  📓 Flip Stats │
│  🛡️ SAFE    💸 PROFIT      🔄 NORMAL    │
│  ⏸️ หยุด    ▶️ รันต่อ      💥 ปิด Position│
│  🔄 Restart              🛑 Stop Bot   │
└─────────────────────────────────┘
```

| ปุ่ม | กลุ่ม | ผลลัพธ์ |
|---|---|---|
| `📊 พอร์ต` | ข้อมูล | Dashboard ครบ + Regime + Trade OBI + Whale Signal |
| `💰 กำไรวันนี้` | ข้อมูล | Income History |
| `📓 Flip Stats` | ข้อมูล | สถิติ OBI Flip Win Rate แยก Regime (L8) |
| `🛡️ SAFE` | Mode | ถอยระยะ +50–200%, ลด TP |
| `💸 PROFIT` | Mode | ลด TP 50% ปิดงานเร็ว |
| `🔄 NORMAL` | Mode | บอทตัดสินใจตามตลาดเอง + **Reset Equity Kill Switch** |
| `⏸️ หยุด` | ควบคุม | Pause bot (ไม่เปิดไม้ใหม่) |
| `▶️ รันต่อ` | ควบคุม | Resume bot + **Reset Equity Kill Switch** |
| `💥 ปิด Position` | ควบคุม | Full Close ทันที |
| `🔄 Restart` | ระบบ | pm2 restart bot |
| `🛑 Stop Bot` | ระบบ | pm2 stop bot |

---

## 📊 12. Dashboard ตัวอย่าง

```
🏰 COMMANDER DASHBOARD v2.0
━━━━━━━━━━━━━━━━━━
📡 Status: 🟢 ONLINE | Mode: 🛡️ SAFE
💰 Balance: $384.95 ($55.78 avail)
⚙️ Grid Step: 0.750% | TP: +3.19% (Net)
━━━━━━━━━━━━━━━━━━
🚀 POSITION: 📈 LONG | 0.072 BTC
├ ❄️ PNL: $-158.66 (-48.25%)
├ Entry: $68,510.54
├ Mark:  $66,327.70
└ Net BE: $68,558.52
━━━━━━━━━━━━━━━━━━
🔮 ACTION PLAN: (DCA Strategy)
├ Layer: 10/12
├ Buy Next: $65,334.5
└ Predicted Avg: $68,205.4
━━━━━━━━━━━━━━━━━━
🧭 Regime: ↔️ CHOPPY (0.0000% range) | Trade OBI: -0.45
━━━━━━━━━━━━━━━━━━
🐳 Whale Signal: กำลังเชื่อมต่อ Order Book...
```

---

## 🚀 13. การเริ่มต้นใช้งาน

### 13.0 Infrastructure
บอทรันบน **Windows (Local)** หรือ **Google Cloud Always Free VM**:

| Field | Value |
|---|---|
| ชื่อเครื่อง | commander-v2-bot |
| Region | us-west1-a (Oregon) |
| Spec | e2-micro (2 vCPU / 1 GB RAM) |
| OS | Ubuntu 22.04 LTS / Windows 11 |
| External IP | 8.229.111.0 |

### 13.1 ติดตั้ง
```bash
pip install -r requirements.txt
```

### 13.2 ตั้งค่า `.env`
```env
GL_API_KEY=your_binance_api_key
GL_API_SECRET=your_binance_api_secret
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
BINANCE_PROXY=http://user:pass@host:port   # optional

# Cloudflare Worker Proxy (เปิดใช้เมื่อ VPS IP ถูก Binance block)
# ถ้าไม่ต้องการใช้ให้ comment บรรทัด CF_WORKER_URL ออก
CF_WORKER_URL=https://binance-proxy.regency2919.workers.dev/proxy
CF_PROXY_SECRET=commander_proxy_secret_2026
```

### 13.3 รันบอท
```bash
# Windows (Local)
python main_commander.py

# Linux VPS ผ่าน pm2 (แนะนำ)
pm2 start main_commander.py --interpreter python --name bot
pm2 save
pm2 logs bot
pm2 restart bot
```

### 13.4 ซิงค์โค้ดใหม่ขึ้น VPS
```bash
cd binance-bot
git pull
pm2 restart bot
```

---

## 🔬 14. หลักการ Microstructure ที่บอทใช้

| แนวคิด | การนำมาใช้ในบอท |
|---|---|
| **L2 Data Limitation** | Time-proxy (Gate 3) แทน CER — Binance ไม่เปิด L3 |
| **Regime Anchor** | OFI 30-tick buffer ตรึงสภาวะ ไม่ react ทุก tick |
| **Trades Oppose Quotes** | OBI สูง + OFI ติดลบ = Spoofing signature ⚠️ |
| **Slippage Power** | Gate 1 threshold 6x avg = institutional minimum |
| **Quote Life Span** | Gate 3: wall < 3s = Spoof (HFT research) |
| **Liquidity Vacuum** | 4-Gate OBI Flip → Dynamic Kill Switch |
| **Adverse Selection** | Kill Switch หยุดซื้อตอน Whale ขาย ป้องกัน Toxic Flow |
| **Signal Decay** | OBI effective ~5s → filter ไม่ใช่ predictor |
| **Executed Flow** | OBI^T: executed trades ไม่ถูก Spoof |
| **Flickering Liquidity** | Choppy market OBI พลิกไว → Gate 4 กรองออก |
| **Circuit Breaker** | Dynamic Cooldown = Cooling-off Period ระดับสถาบัน ปรับตาม Regime |
| **Self-Learning** | Flip Logger (L8) สะสม Win Rate แยก Regime → ปรับ threshold อนาคต |
| **Execution Probability** | OBI_deep: tick-distance weight สะท้อนโอกาสถูกจับคู่จริง |
| **Deep Order Flow** | OBI_flat vs OBI_deep diverge > 0.25 → Layering/Spoofing ไกล mid |
| **Regime-Aware OBI Flip** | CHOPPY: Bypass Kill Switch (92% FLAT) → TRENDING: Active |
| **Triangular Loss Growth** | Dynamic Lot Sizing ลด 30% ที่ Layer 10+ ชะลอ Drawdown |
| **Volatility/Drift Ratio** | σ/μ Gate ตรวจ Trending Real-time ป้องกัน Falling Knife |
| **Variance Age Risk** | Position ถือนานขึ้น → Variance เพิ่ม Exponential → Age Exit 72h |
| **Max Daily Loss** | Prop Firm-style: ขาดทุนสะสม > $50/วัน → หยุดทั้งวัน (Realized PnL) |
| **Runaway Bot Prevention** | Rapid-Fire Throttle: > 3 orders/60s → บล็อกทันที (Algorithm Malfunction guard) |
| **Exchange Circuit Breaker** | ระดับตลาด: S&P 7%/13%/20% halt, SET 8%/15%/20% halt, LULD รายหุ้น |
| **Cancel-on-Disconnect** | ถ้า Bot disconnect: Cancel All Orders อัตโนมัติป้องกัน orphan orders |
| **ADL (Auto-Deleveraging)** | Binance last resort: ปิด Position ฝั่งกำไรหักล้าง Position ล้มละลาย |
| **IP Geoblocking Bypass** | CF Worker edge proxy — VPS IP ถูก block ใช้ CF edge IP แทน (100k/day ฟรี) |

---

## 🧠 15. Why OBI Flip ≠ Kill Switch ใน CHOPPY Market

### Mechanical vs Informational Signal

| ลักษณะ | CHOPPY | TRENDING/VOLATILE |
|---|---|---|
| ผู้ขับเคลื่อน | HFT / Market Maker (Mechanical) | Informed Trader / Institution (Informational) |
| สาเหตุ OBI Flip | Flickering Quotes, Bait Walls, Risk Management | Liquidity Vacuum, Liquidation Cascade, Breakout |
| ผลต่อราคา | ไม่มีนัยสำคัญ — กลับสมดุลเอง | Permanent Price Impact — เทรนด์วิ่งต่อ |
| Empirical Data (n=25) | **92% FLAT** (±0.1% ใน 5 นาที) | **100% DUMP** ที่ตรวจพบ |
| Kill Switch | **Bypass** — Grid เดินต่อ | **Active** — หยุดซื้อ |

### ทำไม Volatility Spike Kill Switch ยังทำงานทุก Regime

OBI Flip ใน CHOPPY อาจเป็น Noise แต่ **Volatility Spike** คือสัญญาณเตือน Regime Transition:
```
CHOPPY → TRENDING = inst_vol พุ่งสูง ≥ 2 ครั้งติดต่อกัน
```
ระบบจึงแยก trigger ออกจากกัน:
- **OBI Flip Kill Switch** → Regime-Aware (bypass ใน CHOPPY)
- **Volatility Spike Kill Switch** → ทุก Regime ไม่มีข้อยกเว้น
- **VolatilityGate** → σ/μ Real-time (Layer ใหม่ที่เพิ่มเข้ามา)

### เกราะป้องกันที่ยังคงอยู่ใน CHOPPY

แม้ OBI Flip จะ bypass แต่บอทยังมีระบบป้องกันชั้นอื่น:
1. **OFI Anchor** — entry gate ต้องผ่าน OFI confirmed ก่อนเปิดไม้
2. **Volatility Spike KS** — ตรวจ inst_vol ทุก loop
3. **VolatilityGate** — σ/μ < 5.0 → บล็อก DCA ทันที
4. **Auto-Monitor** — Layer ≥ 6 → SAFE mode อัตโนมัติ
5. **Anti-Falling Knife** — ไม่เปิดไม้แรกถ้า inst_vol > 0.4%
6. **Equity Kill Switch** — Drawdown > 30% → หยุด + ยกเลิก Orders

---

## 📝 16. Changelog

### v2.2 (2026-03-29) — Cloudflare Worker Proxy (IP Bypass)

**Cloudflare Worker Proxy**
- `cloudflare-proxy/worker.js` — Worker ที่ forward request จาก VPS → CF edge → Binance Futures API
- `cloudflare-proxy/wrangler.toml` — config deploy, `placement.mode = "smart"` (edge ใกล้ที่สุด)
- Auth: `X-Proxy-Secret` header ตรวจสอบก่อน forward (ป้องกัน abuse)
- Route: `GET /health` → status; `ANY /proxy/*` → forward ไป `https://fapi.binance.com/*`
- Deployed: `https://binance-proxy.regency2919.workers.dev/proxy`
- Free tier: 100,000 requests/day
- เปิดใช้: ตั้ง `CF_WORKER_URL` ใน `.env` / ปิดใช้: comment บรรทัดออก
- `binance_global/async_client.py`: รับ `cf_worker_url` + `cf_proxy_secret` ใน `__init__()`, เปลี่ยน URL เมื่อ `_use_cf_proxy=True`
- `shared/config.py`: เพิ่ม `CF_WORKER_URL` และ `CF_PROXY_SECRET` จาก env

---

### v2.1 (2026-03-29) — Crisis Management & Institutional Risk Controls

**Dynamic Lot Sizing (Downward Protection)**
- `_get_lot_scale()` — คืน multiplier ตาม Layer (1.0 → 0.75 → 0.50 → 0.30)
- `LOT_SCALE_BY_LAYER` dict — Layer 0:100%, 6:75%, 8:50%, 10:30%
- `lot_u = base_lot × _get_lot_scale()` ในทุก trading loop
- ป้องกัน Triangular Loss Growth เมื่อ DCA ลึก

**VolatilityGate (σ/μ Real-time)**
- `_check_volatility_gate()` — คำนวณ σ/μ จาก price_history 20 entries
- σ = std dev of log returns | μ = mean absolute MA change
- σ/μ < 5.0 → `_vgate_blocked = True` → บล็อก DCA ทันที
- Re-arm ต้องผ่าน σ/μ ≥ 5.0 ติดต่อกัน 3 รอบ (ป้องกัน Oscillation)
- ใช้กรองทั้งไม้แรก (p_amt == 0) และ DCA (p_amt > 0)

**Equity Kill Switch + Cancel All Orders**
- `EQUITY_DRAWDOWN_LIMIT = -0.30` — Drawdown > 30% → Active
- `EMERGENCY_BAL_LIMIT = 30.0` — Available < $30 → Block DCA
- เมื่อ Active: เรียก `client.cancel_all_open_orders()` คืน Margin ทันที
- Reset ได้ด้วย `🔄 NORMAL` หรือ `▶️ รันต่อ` ใน Telegram
- เพิ่ม `cancel_all_open_orders()` ใน `binance_global/async_client.py`

**CRITICAL SAFE Mode (Layer 10+)**
- Grid Step Multiplier เพิ่มจาก **1.5x → 3.0x** เมื่อ Layer ≥ 10
- รอ Mean-reversion ที่ไกลกว่ามาก ป้องกัน DCA รัวในเทรนด์

**Variance Age Exit (72 ชั่วโมง)**
- `AGE_EXIT_HOURS = 72` — บังคับปิด Position เมื่อถือเกิน
- `_position_open_time` — track เวลาเปิด Position
- Reset อัตโนมัติเมื่อ Position ปิด (p_amt == 0)
- แจ้งเตือน Telegram พร้อม PNL ก่อนปิด

**TRENDING Cooldown เพิ่มขึ้น**
- `KS_COOLDOWN_TRENDING`: 120s → **300s (5 นาที)**
- ป้องกันบอทรับมีดซ้ำขณะ Momentum ยังไม่หมด

**Max Daily Loss Tracker**
- `MAX_DAILY_LOSS = -50.0` — หยุดเทรดทั้งวันเมื่อขาดทุนสะสม > $50
- `_record_realized_pnl()` — บันทึก Realized PnL หลังปิด Position ทุกครั้ง
- `_reset_daily_loss_if_new_day()` — reset counter อัตโนมัติเมื่อขึ้นวันใหม่ (UTC)
- `_daily_kill_active` — block ทั้งไม้แรกและ DCA ตลอดวัน

**Rapid-Fire Order Throttle**
- `THROTTLE_MAX_ORDERS = 3` — สูงสุด 3 orders ใน 60 วินาที
- `_check_order_throttle()` — sliding window timestamp check
- บล็อก + แจ้งเตือน Telegram เมื่อ trigger
- ป้องกันบอทส่ง order รัวผิดปกติ (algorithm malfunction)

---

### v2.0 (2026-03-27) — Institutional-Grade Foundation

**Priority 1: Trade-based OBI (OBI^T)**
- `stream_aggtrade()` ใน `async_client.py` — WebSocket `btcusdt@aggTrade` ทุก 100ms
- `aggtrade_callback()` + `_update_trade_obi()` — O(1) Rolling Window 30s ด้วย `deque`
- OBI Flip Gate 4: Trade OBI < -0.15 เป็น Ultimate Confirmation

**Priority 2: Active Regime-Aware Kill Switch**
- `_get_regime()` — CHOPPY/TRENDING/VOLATILE/WARMING/ERROR
- `_get_dynamic_cooldown()` — ปรับ KS_COOLDOWN อัตโนมัติตาม Regime
- CHOPPY Bypass: OBI Flip ใน CHOPPY = Mechanical Noise (92% FLAT empirical)

**Priority 3: Outbound Token Bucket**
- capacity=3, refill=1/sec, queue max=10

**Priority 4: Flip Logger (Self-Learning)**
- `_flip_log` deque maxlen=100
- `_process_flip_outcomes()` — schedule outcome check 5 นาที
- ปุ่ม `📓 Flip Stats` ใน Telegram menu

**Priority 5: OBI Deep (Distance-Weighted, Top 5)**
- `_obi_deep` — tick distance weighting Top 5 levels
- Gate เพิ่มใน `obi_ok` และ `obi_dca_ok`

**LOB Event-based Sync**
- ลบ REST snapshot dependency (แก้ Windows latency)
- Gap validation: < 50k ข้ามไป, ≥ 50k reset

---

---

## ⚡ 17. Kill Switch Theory — ระบบ 2 ระดับ

### 17.1 ระดับบอทเทรด (Algorithmic Kill Switch)

Kill Switch คือ "Circuit Breaker สำหรับ Algorithm" — ตัดการทำงานทันทีเมื่อความเสี่ยงทะลุเพดาน

**Triggers ที่ COMMANDER v2.1 ใช้:**

| Trigger | เงื่อนไข | Cooldown / Action |
|---|---|---|
| **Volatility Spike** | inst_vol > 1% ติด 2 รอบ | Dynamic Cooldown ตาม Regime |
| **OBI Flip (4 Gates)** | Triple-Verified Flip | 60–300s + Cancel Orders |
| **CUSUM Breakout** | Trend Detection (CPD) | Dynamic Cooldown + Cancel Orders |
| **Equity Kill Switch** | Drawdown > 30% | Block DCA + Cancel All Orders |
| **Emergency Balance** | Available < $30 | Block DCA (ป้องกัน Margin แตก) |
| **VolatilityGate** | σ/μ < 5.0 | Block DCA จนกว่า Re-arm 3 bars |
| **Max Daily Loss** | Realized Loss > $50/วัน | Block ทั้งวันจนเที่ยงคืน UTC |
| **Rapid-Fire Throttle** | > 3 orders / 60s | Block ชั่วคราว แจ้งเตือน |
| **Variance Age Exit** | Position > 72 ชั่วโมง | บังคับปิด Position |

**Dynamic Adaptation (Regime-Aware):**
```
Kill Switch ไม่ได้ตอบสนองเหมือนกันทุกสถานการณ์
CHOPPY   → OBI Flip Bypassed (Noise) + Cooldown สั้น 60s
TRENDING → Kill Switch Active + Cooldown 300s (Full Recovery)
VOLATILE → Capital Preservation Priority + Cooldown 240s
```

**ลำดับความสำคัญการบล็อก DCA:**
```
1. Daily Kill (สูงสุด — ขาดทุนสะสมทั้งวัน)
2. Rapid-Fire Throttle (ป้องกัน Runaway Algorithm)
3. Equity Kill Switch (Drawdown > 30%)
4. VolatilityGate (σ/μ ต่ำ = Trending)
5. Emergency Balance (< $30)
6. Kill Switch (Volatility/OBI Flip/CUSUM)
```

---

### 17.2 ระดับตลาดหลักทรัพย์ (Exchange Circuit Breakers)

**Market-Wide Circuit Breakers:**

| ตลาด | Level 1 | Level 2 | Level 3 |
|---|---|---|---|
| **S&P 500 (สหรัฐ)** | ลง 7% → หยุด 15 นาที | ลง 13% → หยุด 15 นาที | ลง 20% → หยุดทั้งวัน |
| **SET (ไทย)** | ลง 8% → หยุด 30 นาที | ลง 15% → หยุด 30 นาที | ลง 20% → หยุด 60 นาที |
| **Binance Futures** | ไม่มี Market-Wide halt | LULD รายคู่ | Insurance Fund → ADL |

**Limit Up – Limit Down (LULD) รายหุ้น:**
- หุ้นผันผวนเกินกรอบ (5%/10%/20%) ใน 5 นาที → หยุด 5–10 นาที
- บังคับให้ราคา settle ภายใน band ก่อนเปิดใหม่

**Cancel-on-Disconnect (COD):**
```
บอทหลุดการเชื่อมต่อ → ตลาดยกเลิก Open Orders ทั้งหมดอัตโนมัติ
ป้องกัน Orphan Orders ที่เจ้าของควบคุมไม่ได้
COMMANDER v2.1 มี _cancel_all_open_orders() ทำงานคู่กัน
```

**Auto-Deleveraging (ADL) — Binance Last Resort:**
```
เมื่อ Insurance Fund ไม่พอรับมือ Bankruptcy Position
→ ระบบยึด Position ฝั่งกำไรมาหักล้างโดยอัตโนมัติ
→ ผู้ที่ถูก ADL จะถูกปิด Position ที่ Mark Price โดยไม่มีการเตือน
ADL Risk: ยิ่ง Leverage สูงและ PnL% สูง ยิ่งเสี่ยง ADL
```

---

### 17.3 ความแตกต่างระหว่าง 2 ระดับ

| มิติ | Bot Kill Switch | Exchange Circuit Breaker |
|---|---|---|
| **ผู้ควบคุม** | นักลงทุน / Algorithm | หน่วยงานกำกับ / ตลาด |
| **เป้าหมาย** | ปกป้องพอร์ตส่วนตัว | รักษาเสถียรภาพตลาดรวม |
| **ความเร็ว** | Millisecond (real-time) | วินาที (ราคาต้องผ่าน threshold) |
| **Scope** | เฉพาะ Account ของบอท | ทุก participant ในตลาด |
| **Recovery** | User Reset หรือ Cooldown หมด | เวลาที่กำหนด (15–60 นาที) |
| **ตัวอย่าง COMMANDER** | Equity Kill, CUSUM, VolatilityGate | ADL, COD, LULD (Binance) |

**สรุปหลักการ:**
> *Kill Switch ฝั่งบอท → ตัดก่อนพอร์ตพัง*
> *Circuit Breaker ฝั่งตลาด → เบรกมือให้ทุกฝ่ายได้คิด*
> *ทั้งสองระบบทำงานเสริมกัน ไม่ใช่แทนกัน*

---

## 🌐 18. Cloudflare Worker Proxy (IP Bypass)

เมื่อ Google Cloud VPS IP ถูก Binance block — ส่ง request ผ่าน Cloudflare edge แทน

### 18.1 ปัญหาและวิธีแก้

```
ปัญหา: Google Cloud IP range ถูก Binance กรองออก → API ตอบ 403 / Connection Refused
วิธีแก้: มุด IP ผ่าน Cloudflare Worker (IP = CF Edge ไม่ใช่ Google Cloud)
```

**Architecture Flow:**
```
VPS (8.229.111.0)
    │
    ▼ HTTPS request
Cloudflare Worker  (binance-proxy.regency2919.workers.dev)
    │  X-Proxy-Secret: commander_proxy_secret_2026
    │  X-MBX-APIKEY: forwarded
    ▼
Binance Futures API  (fapi.binance.com)
    │
    ▼ Response
VPS ← Cloudflare ← Binance
```

### 18.2 การตั้งค่า

**`.env` (เปิดใช้):**
```env
CF_WORKER_URL=https://binance-proxy.regency2919.workers.dev/proxy
CF_PROXY_SECRET=commander_proxy_secret_2026
```

**ปิดใช้ (comment ออก):**
```env
# CF_WORKER_URL=https://binance-proxy.regency2919.workers.dev/proxy
# CF_PROXY_SECRET=commander_proxy_secret_2026
```

### 18.3 Deploy Cloudflare Worker

```bash
# 1. ติดตั้ง Wrangler CLI
npm install -g wrangler

# 2. Login Cloudflare
wrangler login

# 3. Register workers.dev subdomain (ครั้งแรกเท่านั้น)
#    ไปที่ https://dash.cloudflare.com → Workers & Pages → Register subdomain

# 4. Set secret
wrangler secret put PROXY_SECRET
# พิมพ์: commander_proxy_secret_2026

# 5. Deploy
cd cloudflare-proxy
wrangler deploy
```

**ผลลัพธ์:**
```
✅ Deployed: https://binance-proxy.regency2919.workers.dev
```

### 18.4 ทดสอบ Worker

```bash
# Health check
curl https://binance-proxy.regency2919.workers.dev/health

# ทดสอบ proxy (ต้องมี secret)
curl -H "X-Proxy-Secret: commander_proxy_secret_2026" \
     "https://binance-proxy.regency2919.workers.dev/proxy/fapi/v1/ping"
```

### 18.5 ข้อจำกัด Free Tier

| ข้อจำกัด | Free Tier | ผลต่อบอท |
|---|---|---|
| Request/day | 100,000 | ~70 requests/นาที (เพียงพอมาก) |
| CPU/request | 10ms | ไม่กระทบ (บอทไม่ใช้ heavy compute) |
| Bandwidth | ไม่จำกัด | ✅ |
| WebSocket | ❌ ไม่รองรับ | WebSocket ยังเชื่อมตรง Binance ตามเดิม |

> **หมายเหตุ:** WebSocket streams (`stream_depth`, `stream_aggtrade`, `stream_user_data`) เชื่อมตรงกับ `wss://fstream.binance.com` เสมอ — CF Worker ใช้เฉพาะ REST API เท่านั้น

### 18.6 ไฟล์ที่แก้ไข

| ไฟล์ | การเปลี่ยนแปลง |
|---|---|
| `cloudflare-proxy/worker.js` | Worker script ใหม่ (Auth + Proxy logic) |
| `cloudflare-proxy/wrangler.toml` | Deploy config (name, smart placement) |
| `binance_global/async_client.py` | รับ `cf_worker_url`, `cf_proxy_secret` — เปลี่ยน URL target |
| `shared/config.py` | เพิ่ม `CF_WORKER_URL`, `CF_PROXY_SECRET` จาก `.env` |
| `.env` | ตั้งค่า Worker URL และ Secret |

---

*COMMANDER v2.2 — Institutional-grade Microstructure + Crisis Management + Cloudflare IP Bypass Edition*
*Stack: Python 3.10+ · asyncio · aiohttp · Binance Futures API (REST + WebSocket L2 + aggTrade) · Cloudflare Workers · Windows / Ubuntu 22.04 LTS · Google Cloud e2-micro*
*Concepts: OBI · OBI_deep · OFI · Trade OBI (OBI^T) · 3-Gate Spoof · Tiered Wall · 4-Gate OBI Flip · Dynamic Kill Switch · Regime Detector · VolatilityGate (σ/μ) · Dynamic Lot Sizing · Equity Kill Switch · Variance Age Exit · Max Daily Loss · Rapid-Fire Throttle · Flip Logger · Auto-Monitor · Token Bucket · Circuit Breaker · ADL · COD · CF Worker IP Bypass*
