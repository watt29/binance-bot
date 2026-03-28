# 🏰 COMMANDER v2.0: INSTITUTIONAL-GRADE MICROSTRUCTURE BOT
ระบบบอทเทรด Binance Futures ที่ออกแบบตามหลัก **Market Microstructure** ระดับสถาบัน ผสาน Order Book Analysis, Whale Signal, Spoof Detection, Trade-based OBI, Regime Detection, Dynamic Kill Switch, Flip Logger และ Auto-Monitor ไว้ในไฟล์เดียว (`main_commander.py`)

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
| **Flip Logger** | บันทึกทุก OBI Flip + ตรวจ outcome 5 นาทีทีหลัง (Self-Learning) |
| **Whale Signal** | Tiered Wall Classification (WATCH/STRONG/MEGA) |
| **OBI Flip Alert** | Triple-Verified Early Warning (4 Gates) ตรวจจับ Liquidity Vacuum |
| **Kill Switch** | Dynamic Cooldown ตาม Regime (CHOPPY 60s / TRENDING 120s / VOLATILE 240s) |
| **Auto-Monitor** | ตรวจพอร์ต 6 rules อัตโนมัติทุก 60 วินาที |
| **Telegram Interface** | รับคำสั่ง + ส่ง Dashboard ผ่าน Outbound Token Bucket |
| **SAE (Survivability)** | บริหาร API Weight ป้องกัน IP Ban |

---

## 🧱 2. 9-Layer Institutional Architecture

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
| L7 | **Flip Logger** | บันทึก OBI Flip events + outcome เพื่อ self-learning pattern |

### เสา 4: Risk Management & Survival
| Layer | ชื่อ | หน้าที่ |
|---|---|---|
| L8 | **OBI Flip Alert** | Early Warning (4 Gates) + Dynamic Kill Switch ตาม Regime |
| L9 | **Auto-Monitor** | ตรวจพอร์ต 6 rules ป้องกัน Adverse Selection |

---

## 🧠 3. กลยุทธ์การเทรด (Core Strategy)

### 3.1 Ultra-Adaptive Grid (DCA สูงสุด 12 ไม้)
- **Grid Step** คำนวณจากความผันผวน 24 ชม. (0.5%–2.0%) และ Instantaneous Volatility จาก price_buffer
- ถ้า price_buffer ยังไม่พอ (< 2 entries หลัง restart): ใช้ `vol_24h / 24` เป็น proxy inst_vol แทน
- ตั้งแต่ไม้ที่ 4+ ระยะห่างขยาย ×1.25, ×1.50... (Deep DCA Guard)
- SAFE mode: ถอยระยะไม้ห่างขึ้น 50% อัตโนมัติเมื่อพอร์ตวิกฤต
- Auto-recover mode จาก Layer count เมื่อ restart (ไม่ reset เป็น NORMAL)

**สูตรคำนวณ Grid Step:**
```
base_step   = max(0.5, min(2.0, vol_24h / 8))
inst_vol    = (max(price_buffer) - min(price_buffer)) / min(price_buffer) × 100
grid_step   = base_step + (inst_vol × 0.8)

SAFE mode:  grid_step × 1.5
```

### 3.2 Trailing Stop (Full Close)
- เปิด Trailing เมื่อราคาถึง Take Profit target
- ปิด Position ทั้งหมดเมื่อราคาหลุด Peak × 0.9995
- **OBI Boost**: ถ้า OBI < -0.6 → ปิดเร็วขึ้น (Peak × 0.9998)

### 3.3 Strategy Modes
| Mode | เงื่อนไข | พฤติกรรม |
|---|---|---|
| **NORMAL** | Layer < 6, ไม่มีวิกฤต | เทรดปกติ Grid Step เต็ม |
| **SAFE** | Layer ≥ 6 (auto) หรือสั่งมือ | ถอยระยะ 50%, ลด TP |
| **PROFIT** | กำไร ≥ $2, Layer < 6 | ลด TP 50% ปิดงานเร็ว |

---

## 🐳 4. Whale Signal System (Order Book Analysis)

### 4.1 OBI — Order Book Imbalance (Limit Order-based, Flat Weight)
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

### 4.2 OBI Deep — Distance-Weighted OBI (Top 5 Levels)
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

**ทำไมใช้ Top 5 + Tick Distance:**
- Top 5 = จุดสมดุล depth/noise ที่ดีที่สุด (งานวิจัย HFT) — level 6–20 เพิ่ม noise ไม่เพิ่ม signal
- Tick distance สะท้อน Execution Probability จริง (ใกล้ mid = โอกาสถูกจับคู่สูง)
- % distance ใช้ไม่ได้: BTC spread แคบ ~0.01% ทำให้ทุก level ได้ weight ≈ 0.99 (ไม่ต่างกัน)

**Spoof Detection จาก OBI_flat vs OBI_deep:**
```
OBI_flat สูง + OBI_deep ต่ำ → กำแพงอยู่ไกล mid → สัญญาณ Layering/Spoofing
gap = OBI_flat - OBI_deep > 0.25 → แสดง 🔍(Deep diverge — wall ไกล mid) ใน Dashboard
```

**ใช้งานใน Decision Logic:**
| Gate | เงื่อนไข | bypass |
|---|---|---|
| ไม้แรก | `OBI_flat ≥ -0.30` AND `OFI confirmed` AND `OBI_deep ≥ -0.35` | data ยังไม่พร้อม |
| DCA | `OBI_flat ≥ -0.30` AND `OFI confirmed` AND `OBI_deep ≥ -0.35` | layer ≥ 10 หรือ data ไม่พร้อม |

threshold Deep ผ่อนกว่า flat (-0.35 vs -0.30) เพราะ Top 5 อาจ noisy ช่วง spread กว้าง

### 4.3 OFI — Order Flow Imbalance (Stabilizer)
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

### 4.4 Trade OBI — OBI^T (Executed Trade-based)
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

### 4.5 3-Gate Spoof Detector
| Gate | เงื่อนไข | หลักการ |
|---|---|---|
| **Gate 1** | Volume ≥ 6× avg | กำแพงจริงต้องหนักกว่าสภาพคล่องปกติ |
| **Gate 2** | ห่างจาก mid-price ≤ 0.5% | ไกลกว่านี้ = Bait Wall ล่อรายย่อย |
| **Gate 3** | อยู่นาน ≥ 3 วินาที | หายเร็ว = Spoof (Quote Life Span สั้น) |

### 4.6 Tiered Wall Classification
| Icon | Tier | Threshold | ความหมาย |
|---|---|---|---|
| 🟡 | WATCH | ≥ 6× avg | Retail Whale |
| 🟠 | STRONG | ≥ 8× avg | Institutional |
| 🔴 | MEGA | ≥ 15× avg | S-class / OTC level |

### 4.7 OBI Flip Alert — Triple Verified (4 Gates)
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

**ตัวอย่าง Cooldown หมด:**
```
✅ Kill Switch: cooldown หมดแล้ว กลับมาเทรดปกติครับ
🧭 Regime: CHOPPY | Cooldown: 60s
```

**Diagnostic Log (เมื่อ Gate 4 Block):**
```
🟡 OBI FLIP BLOCKED by TradeOBI: +0.09 (Regime CHOPPY 0.02%) — Flickering Liquidity ignored
```

---

## 🧭 5. Regime Detector (Active)

ตรวจจับสภาวะตลาดจาก price_buffer — ใช้งานจริงในการปรับ Dynamic Cooldown ของ Kill Switch:

```python
rng = (max(price_buffer) - min(price_buffer)) / min(price_buffer) × 100
```

| Regime | เงื่อนไข | Icon | KS Cooldown | ความหมาย |
|---|---|---|---|---|
| **CHOPPY** | rng < 0.15% | ↔️ | 60s | Sideways ผันผวนน้อย Liquidity กลับเร็ว |
| **TRENDING** | 0.15% ≤ rng ≤ 0.8% | 📈 | 120s | มีทิศทาง รอ Momentum หมดแรงก่อน |
| **VOLATILE** | rng > 0.8% | ⚡ | 240s | ผันผวนสูง Capital Preservation สูงสุด |
| **WARMING** | buffer < 2 entries | 🌡️ | 60s | เพิ่งเริ่ม รอข้อมูล (~10s) |
| **ERROR** | Exception | ⚠️ | 60s | ข้อผิดพลาดภายใน |

แสดงใน Dashboard:
```
🧭 Regime: 📈 TRENDING (0.1511% range) | Trade OBI: +0.33
```

---

## 📓 6. Flip Logger — Self-Learning Pattern Engine

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

**ตัวอย่าง Flip Stats (กด `📓 Flip Stats` ใน Telegram):**
```
📓 Flip Log Stats (8 events)
  CHOPPY  → 🟢 BOUNCE 75% | 🔴 DUMP 12% | ⚪ FLAT 12% (n=8)
  TRENDING → 🟢 BOUNCE 50% | 🔴 DUMP 50% | ⚪ FLAT 0% (n=2)
─ ล่าสุด 3 events ─
  21:01 [CHOPPY] BOUNCE +0.182% | OBI +1.00→-0.96 T:-0.25
  20:56 [TRENDING] DUMP -0.073% | OBI +0.69→-0.38 T:-0.30
  20:50 [CHOPPY] BOUNCE +0.123% | OBI +0.88→-0.93 T:-0.32
```

**วิธีใช้ประโยชน์:**
- สะสม 20–30 events → รู้ Win Rate จริงแยกตาม Regime
- BOUNCE สูงใน CHOPPY → Cooldown 60s อาจสั้นเกินไป (ยังมีโอกาสซื้อ)
- DUMP สูงใน VOLATILE → Cooldown 240s คุ้มค่า ไม่ต้องลด
- ปรับ threshold และ cooldown ในอนาคตด้วยหลักฐานจากตลาดจริง (Data-Driven)

---

## 🤖 7. Auto-Monitor (6 Rules)

ตรวจพอร์ตอัตโนมัติทุก 60 วินาที:

| Rule | เงื่อนไข | Action |
|---|---|---|
| 1 | กำไร ≥ $5 | ปิด Position อัตโนมัติทันที |
| 2 | กำไร ≥ $2 + Layer < 6 | เปลี่ยนเป็น PROFIT mode |
| 3 | ขาดทุน ≤ -$120 | แจ้งเตือน Telegram ฉุกเฉิน |
| 4 | Layer ≥ 8 | บังคับ SAFE mode |
| 5 | Layer ≥ 10 | บังคับ SAFE + ถอยระยะ 50% |
| 6 | ราคาใกล้ Liquidation ≤ 5% | แจ้งเตือน Liq proximity ฉุกเฉิน |

---

## 🛡️ 8. ระบบความปลอดภัย (Safety Systems)

### 8.1 Dynamic Kill Switch (Regime-Aware)
**Trigger 1 — Volatility Spike:**
- inst_vol > 1.0% สะสม ≥ 2 ครั้งติดต่อกัน → Kill Switch ON
- Cooldown: Dynamic ตาม Regime

**Trigger 2 — OBI Flip (4 Gates):**
- ผ่านครบ 4 Gates → Kill Switch ON พร้อมแจ้ง Telegram

**Dynamic Cooldown (ปรับอัตโนมัติตาม Regime จริง ณ ขณะนั้น):**
| Regime | Icon | Cooldown | เหตุผล |
|---|---|---|---|
| **CHOPPY** | ↔️ | 60s | Liquidity กลับเร็ว ไม่เสียโอกาส |
| **TRENDING** | 📈 | 120s | รอ Momentum หมดแรงก่อน DCA |
| **VOLATILE** | ⚡ | 240s | Capital Preservation สูงสุด |
| **WARMING / ERROR** | 🌡️⚠️ | 60s | ค่าปลอดภัย รอข้อมูลครบ |

### 8.2 Outbound Token Bucket (Telegram Rate Limiter)
ป้องกัน Telegram API Error 429:
```
Capacity:    3 tokens  (burst สูงสุด 3 ข้อความ)
Refill Rate: 1 token/วินาที
Queue:       สูงสุด 10 ข้อความ
```

### 8.3 Local Order Book — Event-based Sync
แก้ปัญหา Windows latency (~17,578 update IDs/sec gap):
```
1. รับ WebSocket depth event ตรงๆ ไม่ต้อง REST snapshot
2. Accumulate 10 events → _lob_ready = True
3. gap เล็ก (< 50k) → ข้ามไปต่อ
4. gap ใหญ่ (≥ 50k) → reset และ sync ใหม่
```

### 8.4 SAE (Survivability-Aware Execution)
| Weight Usage | Action |
|---|---|
| > 60% | throttle low-priority (delay 2s) |
| > 80% | throttle non-critical (delay 3s) |
| > 95% | block ทุกคำสั่งยกเว้น trade |
| 429/418 | Backoff อัตโนมัติ ไม่ crash |

### 8.5 Anti-Falling Knife
- ไม่เปิดไม้แรกเมื่อราคา > 85% ของกรอบ 24 ชม.
- ไม่เปิดไม้แรกเมื่อ Instant Volatility > 0.4%

### 8.6 Session Recovery
- Restart → ตรวจ Layer count จาก Position จริง
- Layer ≥ 6 → auto-set SAFE mode ทันที (ไม่ reset)

---

## 📟 9. Telegram Menu

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
| `📓 Flip Stats` | ข้อมูล | สถิติ OBI Flip Win Rate แยก Regime (L7) |
| `🛡️ SAFE` | Mode | ถอยระยะ +50%, ลด TP |
| `💸 PROFIT` | Mode | ลด TP 50% ปิดงานเร็ว |
| `🔄 NORMAL` | Mode | บอทตัดสินใจตามตลาดเอง |
| `⏸️ หยุด` | ควบคุม | Pause bot (ไม่เปิดไม้ใหม่) |
| `▶️ รันต่อ` | ควบคุม | Resume bot |
| `💥 ปิด Position` | ควบคุม | Full Close ทันที |
| `🔄 Restart` | ระบบ | pm2 restart bot |
| `🛑 Stop Bot` | ระบบ | pm2 stop bot |

---

## 📊 10. Dashboard ตัวอย่าง

```
🏰 COMMANDER DASHBOARD v2.0
━━━━━━━━━━━━━━━━━━
📡 Status: 🟢 ONLINE | Mode: 🛡️ SAFE
💰 Balance: $384.76 ($55.78 avail)
⚙️ Grid Step: 0.931% | TP: +3.60% (Net)
━━━━━━━━━━━━━━━━━━
🚀 POSITION: 📈 LONG | 0.072 BTC
├ ❄️ PNL: $-121.28 (-36.88%)
├ Entry: $68,510.54
├ Mark:  $66,844.40
└ Net BE: $68,558.52
━━━━━━━━━━━━━━━━━━
🔮 ACTION PLAN: (DCA Strategy)
├ Layer: 10/12
├ Buy Next: $64,734.3
└ Predicted Avg: $68,144.7
━━━━━━━━━━━━━━━━━━
🧭 Regime: 📈 TRENDING (0.1511% range) | Trade OBI: +0.33
━━━━━━━━━━━━━━━━━━
🐳 Whale Signal: 🐳 STRONG SELL | OBI -0.81 OFI -0.03 | $0.1M vs $1.4M
🕵️ Spoof 3 walls (ระวัง!)
```

---

## 🚀 11. การเริ่มต้นใช้งาน

### 11.1 ติดตั้ง
```bash
pip install -r configs/requirements.txt
```

### 11.2 ตั้งค่า `.env`
```env
GL_API_KEY=your_binance_api_key
GL_API_SECRET=your_binance_api_secret
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
BINANCE_PROXY=http://user:pass@host:port   # optional
```

### 11.3 รันบอท
```bash
# รันผ่าน pm2 (แนะนำ)
pm2 start main_commander.py --interpreter python --name bot
pm2 save
pm2 logs bot
pm2 restart bot
```

---

## 🔬 12. หลักการ Microstructure ที่บอทใช้

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
| **Self-Learning** | Flip Logger (L7) สะสม Win Rate แยก Regime → ปรับ threshold อนาคต |
| **Execution Probability** | OBI_deep: tick-distance weight สะท้อนโอกาสถูกจับคู่จริง — ใกล้ mid = หนักกว่า |
| **Deep Order Flow** | OBI_flat vs OBI_deep diverge > 0.25 → Layering/Spoofing signature ไกล mid |

---

## 📝 13. Changelog

### v2.0 (2026-03-28) — Current

**Priority 1: Trade-based OBI (OBI^T)**
- `stream_aggtrade()` ใน `async_client.py` — WebSocket `btcusdt@aggTrade` ทุก 100ms
- `aggtrade_callback()` + `_update_trade_obi()` — O(1) Rolling Window 30s ด้วย `deque`
- OBI Flip Gate 4: Trade OBI < -0.15 เป็น Ultimate Confirmation
- Diagnostic log เมื่อ Gate 4 block ("Flickering Liquidity ignored")

**Priority 2: Active Regime-Aware Kill Switch**
- `_get_regime()` — CHOPPY/TRENDING/VOLATILE/WARMING/ERROR
- `_get_dynamic_cooldown()` — ปรับ KS_COOLDOWN อัตโนมัติตาม Regime
- OBI Flip Alert แสดง Regime + cooldown จริงในข้อความ
- Kill Switch release แสดง Regime + Cooldown ที่ใช้

**Priority 3: Outbound Token Bucket**
- capacity=3, refill=1/sec, queue max=10
- `_consume_out_token()` → `send_message()` → `_send_raw()` pipeline
- Retry อัตโนมัติเมื่อ 429

**Priority 4: Flip Logger (Self-Learning)**
- `_flip_log` deque maxlen=100 — บันทึกทุก Flip event
- `_flip_pending_outcome` — schedule outcome check ใน 5 นาที
- `_process_flip_outcomes()` — เรียกทุก loop ตรวจ outcome อัตโนมัติ
- `_get_flip_stats()` — สรุป Win Rate แยก Regime
- ปุ่ม `📓 Flip Stats` ใน Telegram menu

**Telegram Menu Redesign**
- จัดกลุ่ม 4 แถวตาม function: ข้อมูล / Mode / ควบคุม / ระบบ
- ชื่อปุ่มสั้นลง กดง่ายขึ้น backward compatible กับคำสั่งเก่า

**LOB Event-based Sync**
- ลบ REST snapshot dependency (แก้ Windows latency)
- Accumulate 10 WebSocket events → `_lob_ready = True`
- Gap validation: < 50k ข้ามไป, ≥ 50k reset

**Bug Fixes**
- Regime UNKNOWN หลัง restart → threshold `< 8` → `< 2`, label `WARMING`
- Grid Step กระโดดลง 0.500% → fallback `inst_vol = vol_24h / 24`
- Regime format `0.000%` → `0.0000%` (4 ตำแหน่ง)

**Priority 5: OBI Deep (Distance-Weighted, Top 5)**
- `_obi_deep` — คำนวณทุก depth event ควบคู่ `_obi_score` (ไม่ replace)
- สูตร: `w_i = qty / (1 + |p - mid| / 0.10)` — tick distance weighting
- Top 5 levels เท่านั้น: จุดสมดุล depth/noise (ตัด level 6–20 ที่เป็น Spoof zone)
- Gate เพิ่มใน `obi_ok` (ไม้แรก) และ `obi_dca_ok` (DCA): `OBI_deep ≥ -0.35`
- DCA bypass ยังคงอยู่เมื่อ layer ≥ 10 (survival mode ไม่ให้ signal ใหม่ขัด)
- Dashboard แสดง `OBI_flat` + `OBI_deep` คู่กัน + Spoof hint เมื่อ gap > 0.25

---

*COMMANDER v2.0 — Institutional-grade Microstructure Edition*
*Stack: Python 3.10+ · asyncio · aiohttp · Binance Futures API (REST + WebSocket L2 + aggTrade)*
*Concepts: OBI · OBI_deep · OFI · Trade OBI (OBI^T) · 3-Gate Spoof · Tiered Wall · 4-Gate OBI Flip · Dynamic Kill Switch · Regime Detector · Flip Logger · Auto-Monitor · Token Bucket*
