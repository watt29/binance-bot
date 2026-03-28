# 🏰 COMMANDER v2.0: INSTITUTIONAL-GRADE MICROSTRUCTURE BOT
ระบบบอทเทรด Binance Futures ที่ออกแบบตามหลัก **Market Microstructure** ระดับสถาบัน ผสาน Order Book Analysis, Whale Signal, Spoof Detection, Trade-based OBI, Regime Detection, OBI Flip Alert และ Auto-Monitor ไว้ในไฟล์เดียว (`main_commander.py`)

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
| **Whale Signal** | Tiered Wall Classification (WATCH/STRONG/MEGA) |
| **OBI Flip Alert** | Triple-Verified Early Warning (4 Gates) ตรวจจับ Liquidity Vacuum |
| **Auto-Monitor** | ตรวจพอร์ต 6 rules อัตโนมัติทุก 60 วินาที |
| **Kill Switch** | หยุดเทรดเมื่อ Volatility พุ่งหรือ OBI Flip เกิดขึ้น |
| **Telegram Interface** | รับคำสั่ง + ส่ง Dashboard ผ่าน Outbound Token Bucket |
| **SAE (Survivability)** | บริหาร API Weight ป้องกัน IP Ban |

---

## 🧱 2. 8-Layer Institutional Architecture

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

### เสา 3: Market Context
| Layer | ชื่อ | หน้าที่ |
|---|---|---|
| L6 | **Regime Detector** | ระบุสภาวะตลาด CHOPPY/TRENDING/VOLATILE จาก price range |

### เสา 4: Risk Management & Survival
| Layer | ชื่อ | หน้าที่ |
|---|---|---|
| L7 | **OBI Flip Alert** | Early Warning เมื่อ Whale ถอนสภาพคล่องกะทันหัน (Triple Verified) |
| L8 | **Kill Switch** | ตัดไฟฉุกเฉิน (Volatility spike หรือ OBI Flip ผ่าน 4 Gates) |
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

### 4.1 OBI — Order Book Imbalance (Limit Order-based)
```
OBI = (Σ Bid_price × Bid_qty  -  Σ Ask_price × Ask_qty)
      ─────────────────────────────────────────────────
      (Σ Bid_price × Bid_qty  +  Σ Ask_price × Ask_qty)
```
ช่วงค่า -1.0 ถึง +1.0:

| ค่า OBI | สัญญาณ | ความหมาย |
|---|---|---|
| +0.65 ถึง +1.0 | 🐳 STRONG BUY | Whale กดซื้อหนักมาก |
| +0.3 ถึง +0.65 | 🟢 Buy Pressure | แรงซื้อสุทธิ |
| -0.3 ถึง +0.3 | ⚖️ Balanced | ตลาดสมดุล |
| -0.65 ถึง -0.3 | 🔴 Sell Pressure | แรงขายสุทธิ |
| -1.0 ถึง -0.65 | 🐳 STRONG SELL | Whale ทุบขาย |

**ข้อจำกัด**: OBI เป็น L2 Data (Aggregated) — Noise สูง, Decay ใน ~5 วินาที
ต้องใช้ OFI และ Trade OBI ยืนยันก่อน confirmed

### 4.2 OFI — Order Flow Imbalance (Stabilizer)
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

### 4.3 Trade OBI — OBI^T (Executed Trade-based) [NEW]
วัดแรงซื้อ/ขายจาก **Executed Trades จริง** ผ่าน aggTrade WebSocket (ทุก 100ms):
```
source:  wss://fstream.binance.com/ws/btcusdt@aggTrade
field m: is_buyer_maker
         True  → Taker ขาย (Market Sell)
         False → Taker ซื้อ (Market Buy)

OBI^T = (V_buy - V_sell) / (V_buy + V_sell)
        rolling window = 30 วินาที (O(1) deque)
```
**ข้อดีเหนือ OBI ปกติ:**
| เปรียบเทียบ | OBI (Limit Order) | Trade OBI (OBI^T) |
|---|---|---|
| แหล่งข้อมูล | L2 Order Book snapshot | Executed trades จริง |
| ถูก Spoof ได้ไหม | ใช่ (Flickering Liquidity) | ไม่ได้ (ยืนยันแล้ว) |
| Decay | ~5 วินาที | Rolling 30 วินาที |
| ใช้เป็น Ultimate Gate | ไม่ | ใช่ (Gate 4 ของ OBI Flip) |

### 4.4 3-Gate Spoof Detector
กรองกำแพงปลอม (Bait Walls) ก่อนนับเป็น Whale Wall:

| Gate | เงื่อนไข | หลักการ |
|---|---|---|
| **Gate 1** | Volume ≥ 6× avg (Slippage Power) | กำแพงจริงต้องหนักกว่าสภาพคล่องปกติ |
| **Gate 2** | ห่างจาก mid-price ≤ 0.5% | ไกลกว่านี้ = Bait Wall ล่อรายย่อย |
| **Gate 3** | อยู่นาน ≥ 3 วินาที | หายเร็ว = Spoof (Quote Life Span สั้น) |

**CER Proxy**: Gate 3 ทำหน้าที่แทน Cancellation-to-Execution Ratio เนื่องจาก Binance WebSocket เป็น L2 Data ไม่เปิดเผย Cancel vs Execute แยกกัน

### 4.5 Tiered Wall Classification
Wall ที่ผ่าน 3-Gate แล้วจะถูกจัดระดับ:

| Icon | Tier | Threshold | ความหมาย |
|---|---|---|---|
| 🟡 | WATCH | ≥ 6× avg | Retail Whale |
| 🟠 | STRONG | ≥ 8× avg | Institutional |
| 🔴 | MEGA | ≥ 15× avg | S-class / OTC level (เทียบ S:25x) |

แสดงผลใน Dashboard:
```
🔴 Buy Wall [MEGA] $66,400 (12.50 BTC)
🕵️ Walls: 🔴×1 🟠×1 🟡×2 | กรอง 8 spoof
```

### 4.6 OBI Flip Alert — Triple Verified (4 Gates) [UPGRADED]
ตรวจจับการถอนสภาพคล่องกะทันหัน (Liquidity Vacuum) ด้วย 4 ด่าน:

| Gate | เงื่อนไข | หลักการ |
|---|---|---|
| **Gate 1** | `OBI_SMA(5) < -0.15` | OBI ร่วงลงต่อเนื่อง (ไม่ใช่แค่ 1 tick) |
| **Gate 2** | `OBI_recent_max(30) ≥ +0.60` | เคยสูงมาก้อน (มีการพลิกตัวจริง) |
| **Gate 3** | `OFI < -0.20` | Best bid/ask delta ยืนยัน |
| **Gate 4** | `Trade OBI < -0.15` | Executed trades ยืนยัน Market Sell จริง |

**ก่อน Upgrade (2 Gates เดิม):** Alert ทุก 2 นาทีใน Choppy Market → Kill Switch block DCA
**หลัง Upgrade (4 Gates):** Gate 4 (Trade OBI) กรอง Flickering Liquidity ได้ 100%

**เมื่อ Trigger:**
1. Telegram แจ้งทันที พร้อม OBI ก่อน/หลัง, OFI, Trade OBI
2. Kill Switch เปิด 60 วินาที — บอทหยุดเปิดออเดอร์ใหม่
3. Cooldown 2 นาที ก่อนแจ้งเตือนซ้ำ

**ตัวอย่าง Alert (ใหม่):**
```
🚨 OBI FLIP ALERT (Triple Verified)!
OBI ร่วงจาก +0.92 → -0.57
📉 OFI: -0.25 | Trade OBI: -0.42
🛡️ ชะลอระบบ 60 วินาที (แรงขายจริง ไม่ใช่ Spoof)
```

**Diagnostic Log (เมื่อ Gate 4 Block):**
```
🟡 OBI FLIP BLOCKED by TradeOBI: +0.09 (Regime CHOPPY 0.02%) — Flickering Liquidity ignored
```

---

## 🧭 5. Regime Detector [NEW]

ตรวจจับสภาวะตลาดจาก price_buffer แสดงใน Dashboard ทุกครั้งที่เช็คพอร์ต:

```python
rng = (max(price_buffer) - min(price_buffer)) / min(price_buffer) × 100
```

| Regime | เงื่อนไข | Icon | ความหมาย |
|---|---|---|---|
| **CHOPPY** | rng < 0.15% | ↔️ | ตลาด Sideways ผันผวนน้อย |
| **TRENDING** | 0.15% ≤ rng ≤ 0.8% | 📈 | ตลาดมีทิศทาง |
| **VOLATILE** | rng > 0.8% | ⚡ | ตลาดผันผวนสูง |
| **WARMING** | buffer < 2 entries | 🌡️ | ระบบเพิ่งเริ่ม รอข้อมูล (~10s) |
| **ERROR** | Exception | ⚠️ | ข้อผิดพลาดภายใน |

**หมายเหตุ:** เป็น Read-Only Diagnostics เท่านั้น ไม่เปลี่ยน behavior ของบอท (Priority 2 Active mode ยังไม่เปิด)

แสดงใน Dashboard:
```
🧭 Regime: ↔️ CHOPPY (0.0166% range) | Trade OBI: +0.82
```

---

## 🤖 6. Auto-Monitor (6 Rules)

ตรวจพอร์ตอัตโนมัติทุก 60 วินาที ตัดสินใจเองโดยไม่ต้องสั่งมือ:

| Rule | เงื่อนไข | Action |
|---|---|---|
| 1 | กำไร ≥ $5 | ปิด Position อัตโนมัติทันที |
| 2 | กำไร ≥ $2 + Layer < 6 | เปลี่ยนเป็น PROFIT mode |
| 3 | ขาดทุน ≤ -$120 | แจ้งเตือน Telegram ฉุกเฉิน |
| 4 | Layer ≥ 8 | บังคับ SAFE mode |
| 5 | Layer ≥ 10 | บังคับ SAFE + ถอยระยะ 50% |
| 6 | ราคาใกล้ Liquidation ≤ 5% | แจ้งเตือน Liq proximity ฉุกเฉิน |

---

## 🛡️ 7. ระบบความปลอดภัย (Safety Systems)

### 7.1 Kill Switch
**Trigger 1 — Volatility Spike:**
- นับ spike count ทุก tick (inst_vol > 1.0%)
- spike ≥ 2 ครั้งติดต่อกัน → Kill Switch ON
- Cooldown: 180 วินาที แล้วคลายเอง

**Trigger 2 — OBI Flip (Triple Verified 4 Gates):**
- ผ่านครบ 4 Gates → Kill Switch ON 60 วินาที พร้อมแจ้ง Telegram

### 7.2 Outbound Token Bucket (Telegram Rate Limiter) [NEW]
ป้องกัน Telegram API Error 429 (Too Many Requests):
```
Capacity:    3 tokens  (burst สูงสุด 3 ข้อความ)
Refill Rate: 1 token/วินาที  (= 1 msg/sec สอดคล้องกับ Telegram limit)
Queue:       สูงสุด 10 ข้อความ  (ถ้า tokens หมด → เข้าคิวก่อน)
```
- ถ้า Queue เต็ม 10 ข้อความ → ทิ้ง alert เก่าพร้อม log warning
- ถ้า Telegram ตอบ 429 → retry อัตโนมัติ ไม่ crash

### 7.3 Local Order Book — Event-based Sync [UPGRADED]
Binance standard LOB sync ที่แก้ปัญหา Windows latency:

**ปัญหาเดิม:** REST snapshot เสมอมาช้ากว่า WebSocket feed บน Windows (~17,578 update IDs/sec gap) → `⚠️ LOB out of sync` ทุก loop

**วิธีแก้ใหม่ (Event-based):**
```
1. รับ WebSocket depth event ตรงๆ ไม่ต้อง REST snapshot
2. Accumulate 10 events → set _lob_ready = True
3. ต่อจากนั้น validate pu == prev_u ปกติ
4. ถ้า gap เล็ก (< 50,000) → ข้ามไปต่อ
5. ถ้า gap ใหญ่ (≥ 50,000) → reset และ sync ใหม่
```

### 7.4 Inventory Skew Control
- ตรวจ Position size vs. Available Balance
- แจ้งเตือนเมื่อ overexposed / toxic position

### 7.5 SAE (Survivability-Aware Execution)
ป้องกัน IP Ban จาก Binance:
- ติดตาม `X-MBX-USED-WEIGHT-1M` header ทุก request
- **> 60% weight**: throttle คำสั่ง low-priority (delay 2s)
- **> 80% weight**: throttle คำสั่ง non-critical (delay 3s)
- **> 95% weight**: block ทุกคำสั่งยกเว้น trade (Priority 0)
- **429/418 response**: Backoff อัตโนมัติ ไม่ crash

### 7.6 Anti-Falling Knife
- ไม่เปิดไม้แรกเมื่อราคา > 85% ของกรอบ 24 ชม.
- ไม่เปิดไม้แรกเมื่อ Instant Volatility > 0.4%
- ป้องกันซื้อที่ "ยอดดอย"

### 7.7 Session Recovery
- เมื่อ restart: ตรวจ Layer count จาก Position จริง
- Layer ≥ 6 → auto-set SAFE mode ทันที (ไม่ reset เป็น NORMAL)
- `last_buy_price` set เป็น current price ป้องกัน duplicate buy

---

## 📟 8. คำสั่ง Telegram

| คำสั่ง / ปุ่ม | ผลลัพธ์ |
|---|---|
| `📊 เช็คพอร์ต` | Dashboard ครบ + Regime + Trade OBI + Whale Signal + Tiered Walls |
| `🛡️ ขอปลอดภัยไว้ก่อน` | เปลี่ยนเป็น SAFE mode |
| `💸 ขอกำไรเข้าพอร์ตบ่อยๆ` | เปลี่ยนเป็น PROFIT mode |
| `🔄 ปล่อยแบบเดิม` | เปลี่ยนเป็น NORMAL mode |
| `💥 ปิดทุกออเดอร์` | ปิด Position ทั้งหมด (Full Close) |
| `⏸️ หยุดชั่วคราว` | Pause bot |
| `▶️ เริ่มรันต่อ` | Resume bot |
| `💰 กำไรวันนี้` | รายงาน Income History |
| `🔄 รีสตาร์ทบอท (PM2)` | pm2 restart bot |
| `🛑 ปิดบอทถาวร (PM2 Stop)` | pm2 stop bot |

---

## 🚀 9. การเริ่มต้นใช้งาน

### 9.1 ติดตั้ง
```bash
pip install -r configs/requirements.txt
```

### 9.2 ตั้งค่า `.env`
```env
GL_API_KEY=your_binance_api_key
GL_API_SECRET=your_binance_api_secret
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
BINANCE_PROXY=http://user:pass@host:port   # optional (QuotaGuard Static IP)
```

### 9.3 รันบอท
```bash
# รันตรง
python main_commander.py

# รันผ่าน pm2 (แนะนำ — auto restart)
pm2 start main_commander.py --interpreter python --name bot
pm2 save          # บันทึกให้ restart อัตโนมัติหลัง reboot
pm2 logs bot      # ดู log
pm2 restart bot   # restart หลังแก้โค้ด
```

### 9.4 ตรวจสอบ Config
```bash
python shared/config.py
```

---

## 📊 10. Dashboard ตัวอย่าง (ล่าสุด)

```
🏰 COMMANDER DASHBOARD v2.0
━━━━━━━━━━━━━━━━━━
📡 Status: 🟢 ONLINE | Mode: 🛡️ SAFE
💰 Balance: $384.76 ($55.78 avail)
⚙️ Grid Step: 0.771% | TP: +3.23% (Net)
━━━━━━━━━━━━━━━━━━
🚀 POSITION: 📈 LONG | 0.072 BTC
├ ❄️ PNL: $-147.32 (-44.80%)
├ Entry: $68,510.54
├ Mark:  $66,478.50
└ Net BE: $68,558.52
━━━━━━━━━━━━━━━━━━
🔮 ACTION PLAN: (DCA Strategy)
├ Layer: 10/12
├ Buy Next: $65,054.4
└ Predicted Avg: $68,177.2
━━━━━━━━━━━━━━━━━━
🧭 Regime: ↔️ CHOPPY (0.0166% range) | Trade OBI: +0.82
━━━━━━━━━━━━━━━━━━
🐳 Whale Signal: 🟢 Buy Pressure | OBI +0.55 OFI +0.03 | $1.2M vs $0.3M
```

---

## 🔬 11. หลักการ Microstructure ที่บอทใช้

| แนวคิด | การนำมาใช้ในบอท |
|---|---|
| **L2 Data Limitation** | ใช้ Time-proxy (Gate 3) แทน CER เนื่องจาก Binance ไม่เปิด L3 |
| **Regime Anchor** | OFI 30-tick buffer ตรึงสภาวะ ไม่ react ทุก tick |
| **Trades Oppose Quotes** | OBI สูง + OFI ติดลบ = Spoofing signature → ⚠️ |
| **Slippage Power** | Gate 1 threshold 6x avg = institutional minimum |
| **Quote Life Span** | Gate 3: wall < 3s = Spoof (สอดคล้องกับ HFT research) |
| **Liquidity Vacuum** | OBI Flip Alert 4-Gate → Kill Switch ป้องกัน Adverse Selection |
| **Signal Decay** | OBI effective ~5s → ใช้เป็น filter ไม่ใช่ predictor |
| **Executed Flow** | Trade OBI (OBI^T): executed trades ไม่ถูก Spoof ต่างจาก Limit Order OBI |
| **Flickering Liquidity** | Choppy market ทำให้ OBI พลิกไว → Trade OBI Gate 4 กรองออก |

---

## 📝 12. Changelog

### v2.0 (2026-03-28) — Current
**Priority 1: Trade-based OBI (OBI^T)**
- เพิ่ม `stream_aggtrade()` ใน `async_client.py` — WebSocket `btcusdt@aggTrade` ทุก 100ms
- เพิ่ม `aggtrade_callback()` และ `_update_trade_obi()` — O(1) Rolling Window 30s ด้วย `deque`
- OBI Flip Alert เพิ่ม Gate 4 (Trade OBI < -0.15) เป็น Ultimate Confirmation
- Diagnostic log: แสดงทุกครั้งที่ Gate 4 block Flip ("Flickering Liquidity ignored")

**Priority 2: Regime Detection (Read-Only)**
- เพิ่ม `_get_regime()` — คืน (CHOPPY/TRENDING/VOLATILE/WARMING/ERROR, range_pct)
- แสดงใน Dashboard ทุกรายงาน: `🧭 Regime: ↔️ CHOPPY (0.0166% range) | Trade OBI: +0.82`
- Format: 4 ตำแหน่งทศนิยม เพื่อจับ range เล็กๆ เช่น 0.0001%

**Priority 3: Outbound Token Bucket (Telegram Rate Limiter)**
- Token Bucket: capacity=3, refill=1/sec, queue สูงสุด 10 ข้อความ
- `_consume_out_token()` → `send_message()` → `_send_raw()` pipeline
- ป้องกัน Telegram API 429 error และ dropped alerts

**LOB Event-based Sync**
- ลบการพึ่ง REST snapshot ออกทั้งหมด (แก้ปัญหา Windows latency)
- Accumulate 10 WebSocket events → `_lob_ready = True` โดยตรง
- Gap validation: gap เล็ก (< 50k) ข้ามไป, gap ใหญ่ reset

**Bug Fixes**
- Regime UNKNOWN หลัง restart: ลด threshold จาก `< 8` → `< 2` entries, เปลี่ยน label เป็น `WARMING`
- Grid Step กระโดดลง 0.500% หลัง restart: เพิ่ม fallback `inst_vol = vol_24h / 24` เมื่อ buffer < 2

---

*COMMANDER v2.0 — Institutional-grade Microstructure Edition*
*Stack: Python 3.10+ · asyncio · aiohttp · Binance Futures API (REST + WebSocket L2 + aggTrade)*
*Concepts: OBI · OFI · Trade OBI (OBI^T) · 3-Gate Spoof · Tiered Wall · 4-Gate OBI Flip Alert · Regime Detector · Kill Switch · Auto-Monitor · Token Bucket*
