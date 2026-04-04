# 🏰 COMMANDER v2.4: INSTITUTIONAL-GRADE MICROSTRUCTURE BOT

ระบบบอทเทรด Binance Futures ที่ออกแบบตามหลัก **Market Microstructure** ระดับสถาบัน ผสาน Order Book Analysis, Whale Signal, Spoof Detection, Trade-based OBI, Regime Detection, Dynamic Kill Switch, VolatilityGate, Dynamic Lot Sizing, Equity Kill Switch, Variance Age Exit, Flip Logger, Auto-Monitor, Cloudflare Worker IP Bypass, BNB Fee Burn, Maker Rebate Tracking, OBI Quote Cancellation, Inventory TP Scaling และ **WATCHDOG v1.2** ไว้ในระบบเดียว

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
| **CUSUM Detector** | Change Point Detection — ตรวจ Trend Breakout แบบ Sequential |
| **Flip Logger** | บันทึกทุก OBI Flip + ตรวจ outcome 5 นาทีทีหลัง (Self-Learning) |
| **Whale Signal** | Tiered Wall Classification (WATCH/STRONG/MEGA) |
| **OBI Flip Alert** | Triple-Verified Early Warning (4 Gates) ตรวจจับ Liquidity Vacuum |
| **Kill Switch** | Dynamic Cooldown ตาม Regime (CHOPPY 60s / TRENDING 300s / VOLATILE 240s) |
| **Equity Kill Switch** | หยุด DCA + ยกเลิก Orders เมื่อ Drawdown > 30% |
| **Dynamic Lot Sizing** | ลด Lot อัตโนมัติตาม Layer (100% → 30%) — Downward Protection |
| **Inventory TP Scaling** | Layer 8-9 ลด TP target ให้ออกเร็วขึ้นแต่ยังคุ้มค่า fee |
| **OBI Quote Cancellation** | ยกเลิก GTX order ทันทีเมื่อ OBI พลิกเสียเปรียบหลังวาง (Adverse Selection Guard) |
| **Maker Rebate Tracking** | บันทึกค่า fee ที่ประหยัดได้สะสม (GTX Maker vs Taker baseline) |
| **Variance Age Exit** | บังคับปิด Position เมื่อถือเกิน 72 ชั่วโมง |
| **Auto-Monitor** | ตรวจพอร์ต 6 rules อัตโนมัติทุก 60 วินาที |
| **Telegram Interface** | รับคำสั่ง + ส่ง Dashboard ผ่าน Outbound Token Bucket |
| **SAE (Survivability)** | บริหาร API Weight ป้องกัน IP Ban |
| **Cloudflare Worker Proxy** | มุด IP ผ่าน CF edge เมื่อ VPS ถูก Binance block (100k req/day ฟรี) |
| **BNB Fee Burn** | เปิด BNB fee discount อัตโนมัติตอน startup (ลด fee 25%) |
| **Watchdog Bot v1.2** | ตรวจสอบพฤติกรรมบอทหลัก 14 checks ทุก 30 วินาที + Heartbeat File |

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

| Layer | Mode | Grid Multiplier | เหตุผล |
|---|---|---|---|
| 1–5 | NORMAL | ×1.0 (100%) | เทรดปกติ |
| 6–7 | PRE-SAFE | ×1.25 (125%) | เริ่มระวัง |
| 8–9 | SAFE | ×1.5 (150%) | ถอยระยะ 50% |
| 10–12 | CRITICAL SAFE | ×3.0 (300%) | ป้องกัน Margin แตก |

### 3.3 Inventory TP Scaling (Layer 8-9 เท่านั้น)

Layer 8-9 = Inventory สูง → ลด TP เพื่อออกเร็วขึ้น แต่ยังคุ้มค่า fee

```
fee_floor_pct = (maker_fee × active_layers + maker_fee) × 100 × leverage
min_tp        = fee_floor_pct × 1.3   (กำไรสุทธิ 30% เหนือ fee)
tp_final      = max(min_tp, tp_original × 0.65)
```

| Layer | TP Scale | min TP (Leverage 15x) | หมายเหตุ |
|---|---|---|---|
| 1–7 | 100% (ปกติ) | — | ไม่แตะ |
| **8** | **65%** | **~2.63%** | ออกเร็ว ยังคุ้ม fee |
| **9** | **65%** | **~2.93%** | ออกเร็ว ยังคุ้ม fee |
| 10–12 | 100% (SAFE จัดการ) | — | CRITICAL SAFE ดูแลแทน |

### 3.4 Trailing Stop (Full Close)
- เปิด Trailing เมื่อราคาถึง Take Profit target
- ปิด Position ทั้งหมดเมื่อราคาหลุด Peak × 0.9995
- **OBI Boost**: ถ้า OBI < -0.6 → ปิดเร็วขึ้น (Peak × 0.9998)

### 3.5 Strategy Modes
| Mode | เงื่อนไข | พฤติกรรม |
|---|---|---|
| **NORMAL** | Layer < 6, ไม่มีวิกฤต | เทรดปกติ Grid Step เต็ม |
| **SAFE** | Layer ≥ 6 (auto) หรือสั่งมือ | ถอยระยะ 50–200%, ลด TP |
| **PROFIT** | กำไร ≥ $2, Layer < 6 | ลด TP 50% ปิดงานเร็ว |

### 3.6 GTX (Post-Only) + OBI Quote Cancellation

บอทส่ง order แบบ **GTX (Good Till Crossing)** — Maker Only เพื่อลด fee

```
ถ้า order จะกลายเป็น Taker → Binance return EXPIRED → ไม่นับว่าสำเร็จ

หลังวาง GTX: ตรวจ OBI ทุก loop (5s) ใน window 3–30 วินาที
ถ้า OBI < -0.45 → cancel_all_open_orders ทันที (Adverse Selection Guard)
```

| ขั้นตอน | กลไก |
|---|---|
| วาง GTX | บันทึก `_pending_gtx_order` (orderId, price, timestamp) |
| OBI พลิก < -0.45 | ยกเลิก order + แจ้ง Telegram |
| OBI ปกติ / หลัง 30s | ล้าง pending (fill แล้วหรือ EXPIRED) |

### 3.7 Maker Rebate Tracking

```
rebate_per_trade = (taker_fee - maker_fee) × notional
                 = (0.000375 - 0.00015) × notional
                 = 0.000225 × notional

สะสมใน _rebate_saved_total ตลอด session
แสดงใน Dashboard: 💰 Maker Rebate Saved: $X.XXXX (N trades, Xh)
```

**ประหยัดต่อรอบ (DCA 8 ชั้น + 1 ปิด, Notional ~$3,800):**
- Taker ทั้งหมด: 9 × 0.05% = $17.10
- Maker + BNB: 9 × 0.015% = $5.13
- **ประหยัด: ~$11.97 ต่อ round-trip**

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
base_lot   = max(200, min((wallet_balance × 0.8 / 5) × leverage, 500))
lot_scale  = _get_lot_scale()     # ตาม Layer ปัจจุบัน
actual_lot = base_lot × lot_scale

Layer Count = round(positionAmt × entryPrice / base_lot)  ← ใช้ base_lot เสมอ
              ไม่ใช่ actual_lot (ป้องกัน circular dependency)
```

---

## 🌡️ 5. VolatilityGate (σ/μ Ratio Real-time)

ตรวจจับ Regime แบบ Real-time ด้วยอัตราส่วน Volatility ต่อ Drift:

```
σ = std dev ของ log returns (ความผันผวน)
μ = mean absolute MA change (ทิศทาง Drift)

σ/μ สูง  → CHOPPY  (Volatility ครอบงำ)  → ปลอดภัยสำหรับ Grid
σ/μ ต่ำ  → TRENDING (Drift ครอบงำ)     → อันตราย บล็อก DCA
```

| สภาวะ | σ/μ Ratio | Action |
|---|---|---|
| **CHOPPY (ปลอดภัย)** | ≥ 5.0 | Grid ทำงานปกติ |
| **TRENDING (อันตราย)** | < 5.0 | บล็อก DCA ทันที |

Re-arm ต้องผ่าน σ/μ ≥ 5.0 ติดต่อกัน **3 รอบ** (ป้องกัน Whipsaw)

---

## 🐳 6. Whale Signal System (Order Book Analysis)

### 6.1 OBI — Order Book Imbalance (UQ: Unbalanced Quoting)
```
OBI_flat = (Σ Bid_p×Bid_q − Σ Ask_p×Ask_q) / (Σ Bid_p×Bid_q + Σ Ask_p×Ask_q)
```

ทำหน้าที่เป็น **UQ (Unbalanced Quoting) Detector** — ตรวจความไม่สมดุลเทียมใน Order Book
Wall ปลอม (Spoof/Layering) ต้องสร้าง UQ สูงก่อนเสมอ → OBI เป็น gate หลักที่แม่นยำที่สุด

| ค่า OBI | สัญญาณ |
|---|---|
| +0.65 → +1.0 | 🐳 STRONG BUY |
| +0.3 → +0.65 | 🟢 Buy Pressure |
| -0.3 → +0.3 | ⚖️ Balanced |
| -0.65 → -0.3 | 🔴 Sell Pressure |
| -1.0 → -0.65 | 🐳 STRONG SELL |

### 6.2 OBI Deep — Distance-Weighted OBI (Anti-Layering)
```
w_i      = 1 / (1 + d_i)   d_i = |price_i − mid| / tick_size
OBI_deep = (Σ w_i×Bid_q − Σ w_i×Ask_q) / (Σ w_i×Bid_q + Σ w_i×Ask_q)
```
- Wall ที่อยู่ไกล mid → weight ต่ำ → Spoofer ที่วาง Wall ลึกเพื่อหลีกเลี่ยง execution risk ถูก under-weight โดยอัตโนมัติ
- `OBI_flat สูง + OBI_deep ต่ำ` → กำแพงอยู่ไกล mid → สัญญาณ Layering/Spoofing

### 6.3 OFI — Order Flow Imbalance (Stabilizer, 30-tick buffer)
- `bid ขึ้น / qty เพิ่ม` → +1 (buy flow)
- `ask ลด / qty ลด` → +1 (buy flow)
- บทบาท: Anchor Regime — ตรึงสภาวะตลาด ไม่ react ตาม Noise ทุก tick

### 6.4 Trade OBI — OBI^T (Executed Trade-based, Rolling 30s)
```
source: btcusdt@aggTrade | field m: is_buyer_maker
OBI^T = (V_buy − V_sell) / (V_buy + V_sell)
```
- ไม่ถูก Spoof ได้ (Executed trades จริง — ไม่ใช่ Limit Order)
- ทำหน้าที่ **Ultimate Gate** ใน OBI Flip Alert

### 6.5 3-Gate Spoof Detector (AC: Abnormal Cancellation Proxy)
| Gate | เงื่อนไข |
|---|---|
| Gate 1 | Volume ≥ 6× avg |
| Gate 2 | ห่างจาก mid ≤ 0.5% |
| Gate 3 | อยู่นาน ≥ 3 วินาที (Quote Life Span) |

Gate 3 คือ Time-proxy สำหรับ AC Detection — Wall ที่หายภายใน 3s = Spoof

### 6.6 Tiered Wall Classification
| Tier | Threshold |
|---|---|
| 🟡 WATCH | ≥ 6× avg |
| 🟠 STRONG | ≥ 8× avg |
| 🔴 MEGA | ≥ 15× avg |

### 6.7 OBI Flip Alert — Triple Verified (4 Gates)
| Gate | เงื่อนไข |
|---|---|
| Gate 1 | OBI_SMA(5) < -0.15 |
| Gate 2 | OBI_recent_max(30) ≥ +0.60 |
| Gate 3 | OFI < -0.20 |
| Gate 4 | Trade OBI < -0.15 |

---

## 🧭 7. Regime Detector

| Regime | เงื่อนไข | KS Cooldown |
|---|---|---|
| **CHOPPY** | rng < 0.15% | 60s |
| **TRENDING** | 0.15% ≤ rng ≤ 0.8% | 300s |
| **VOLATILE** | rng > 0.8% | 240s |
| **WARMING** | buffer < 2 entries | 60s |

---

## 📓 8. Flip Logger — Self-Learning Pattern Engine

บันทึกทุก OBI Flip event + ตรวจ outcome หลัง 5 นาที (maxlen=100 events):

| Label | เงื่อนไข |
|---|---|
| 🟢 BOUNCE | delta ≥ +0.1% |
| 🔴 DUMP | delta ≤ -0.1% |
| ⚪ FLAT | -0.1% < delta < +0.1% |

**Insight จาก Data จริง (n=99):**
```
CHOPPY   → BOUNCE 18% | DUMP 14% | FLAT 68%  (n=73)
TRENDING → BOUNCE 19% | DUMP 23% | FLAT 58%  (n=26)

KS fired=74 | bypass=25
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
| 5 | Layer ≥ 8 (CRITICAL) | บังคับ SAFE + Grid ×3.0 |
| 6 | Position ถือ ≥ 72 ชั่วโมง | บังคับปิด (Variance Age Exit) |

---

## 🛡️ 10. ระบบความปลอดภัย (Safety Systems)

### 10.1 Dynamic Kill Switch (Regime-Aware)
| Trigger | เงื่อนไข | Cooldown |
|---|---|---|
| Volatility Spike | inst_vol > 1% ติด 2 รอบ | Dynamic ตาม Regime |
| OBI Flip (4 Gates) | Triple-Verified | 60–300s + Cancel Orders |
| CUSUM Breakout | Trend Detection (CPD) | Dynamic + Cancel Orders |

**CHOPPY: Bypass OBI Flip** (68% FLAT empirical) — Grid เดินต่อ
**TRENDING/VOLATILE: Active** — หยุดซื้อ

**หลัง KS Release:**
- Restore `last_buy_price` จาก `entry_p` ทันที (ป้องกัน DCA trigger ทันที)
- Cooldown 30 วินาที ก่อนอนุญาต DCA

### 10.2 Equity Kill Switch
```
Drawdown ≤ -30% → ยกเลิก Open Orders ทั้งหมด + บล็อก DCA
Auto-Reset เมื่อ Drawdown > -15%
Reset ด้วย 🔄 NORMAL หรือ ▶️ รันต่อ
```

### 10.3 Emergency Balance Lock
```
Available < $30 → บล็อก DCA (รักษา Margin)
```

### 10.4 VolatilityGate
```
σ/μ < 5.0 → บล็อก DCA + Re-arm ต้องผ่าน 3 bars ติดกัน
```

### 10.5 Variance Age Exit
```
Position ถือ > 72 ชั่วโมง → บังคับปิดอัตโนมัติ
```

### 10.6 Max Daily Loss (Prop Firm Style)
```
Realized PnL ≤ -$50/วัน → หยุดทั้งวัน
Reset อัตโนมัติ UTC 00:00
```

### 10.7 Rapid-Fire Order Throttle
```
> 3 orders ใน 60s → บล็อก + แจ้ง Telegram
```

### 10.8 Startup Guard
```
ทุก restart → รอ account cache พร้อมก่อน (force fetch + retry 10 ครั้ง)
ป้องกันบอทเปิดไม้ขณะ p_amt ยังโหลดไม่เสร็จ
```

### 10.9 Session Recovery
```
Restart → last_buy_price = entry_p (ไม่ใช่ cur_p)
KS Release → last_buy_price = entry_p + Cooldown 30s
ป้องกัน next_buy_price ≈ cur_p → DCA trigger ทันที
```

### 10.10 OBI Quote Cancellation (Adverse Selection Guard)
```
หลังวาง GTX: monitor OBI ทุก loop ใน window 3–30 วินาที
OBI < -0.45 → cancel_all_open_orders ทันที
หลักการ: ถ้าตลาดพลิกทันทีหลังวาง = Adverse Selection → ออกก่อนถูก fill
```

### 10.11 BNB Fee Burn (Auto)
```
Startup: ตรวจ spotBNBBurn status
ถ้าปิดอยู่ → เปิดอัตโนมัติ (ลด fee 25%)
taker_fee: 0.05% → 0.0375%
maker_fee: 0.02% → 0.015%
```

### 10.12 Outbound Token Bucket (Telegram)
```
Capacity: 3 tokens | Refill: 1/sec | Queue: max 10
```

### 10.13 SAE (API Weight Management)
| Weight | Action |
|---|---|
| > 60% | Delay 2s |
| > 80% | Delay 3s |
| > 95% | Block ทุกคำสั่งยกเว้น trade |
| 429/418 | Backoff อัตโนมัติ |

### 10.14 Anti-Falling Knife
- ไม่เปิดไม้แรกเมื่อราคา > 85% ของกรอบ 24 ชม.
- ไม่เปิดไม้แรกเมื่อ inst_vol > 0.4%

---

## 🐕 11. WATCHDOG Bot v1.2 (watchdog.py)

บอทแยกต่างหากที่รัน parallel กับบอทหลัก ตรวจสอบ **14 checks** ทุก **30 วินาที**

**หลักการออกแบบ (ป้องกันมโน):**
- ใช้ **timestamp** เสมอ ไม่นับบรรทัด (log rate ต่างกัน)
- keyword ต้อง **specific** กับ format log จริง ไม่ match log อื่น
- ไม่พบ log ≠ error — log rotation คือเรื่องปกติ
- ตรวจเฉพาะเมื่อมี **หลักฐานชัดเจน** (unknown = pass เสมอ)
- **Heartbeat File** (`logs/heartbeat.txt`) แทน log parsing สำหรับ Engine check

### Fix Verification (4 checks)
| Check | ตรวจอะไร | หลักฐานที่ต้องการ |
|---|---|---|
| GTX EXPIRED Fix | EXPIRED ไม่นับว่าสำเร็จ | ไม่มี "เปิด (GTX) สำเร็จ" ภายใน 15s หลัง EXPIRED |
| Circular Layer Fix | Layer ไม่บวมเกิน 12 | Calc log: `= Layers N (M.Ratio` |
| Startup Guard | cache ready ก่อน loop | ตรวจเฉพาะเมื่อมี OPERATIONAL event ใน log |
| BNB Fee Burn | เปิดอยู่ทุก startup | ถ้าไม่มี log = log หมุนแล้ว → pass |

### Strategy Behavior (3 checks)
| Check | ตรวจอะไร | หลักฐานที่ต้องการ |
|---|---|---|
| Open Condition | ทุกไม้มี Prediction ก่อน | timestamp window 10s ก่อนเปิด |
| DCA Layer Limit | ไม่เกิน 12 layers | Calc log format เท่านั้น |
| TP Triggered | ปิดไม้ได้ปกติ | รายงานเท่านั้น ไม่ fail |

### Feature Verification (3 checks)
| Check | ตรวจอะไร |
|---|---|
| Maker Rebate | บันทึกค่าสะสมจาก log จริง |
| OBI Cancel | นับ cancel ใน 1h — >10 = threshold อาจไวเกิน |
| Inventory TP | รายงาน Layer ปัจจุบัน |

### Anomaly Detection (4 checks)
| Check | ตรวจอะไร | เกณฑ์ |
|---|---|---|
| Rapid Fire | ระยะห่างระหว่างไม้ | timestamp < 10s = ผิดปกติ |
| Error Rate | ERROR ใน 5 นาทีล่าสุด | `" | ERROR | "` format เท่านั้น |
| WebSocket | หลุดแล้วไม่ reconnect | timestamp ล่าสุด: disconnect vs reconnect |
| **Engine Alive** | **Heartbeat File** | **อ่าน `logs/heartbeat.txt` โดยตรง — ไม่พึ่ง log** |

**Engine Alive — Heartbeat Mechanism:**
```
บอทหลัก → เขียน unix timestamp ลง logs/heartbeat.txt ทุก ~5s
Watchdog → อ่านไฟล์โดยตรง → ถ้า elapsed > 120s → crash alert
ข้อดี: ไม่ขึ้นกับ log rotation เลย — แม่นยำ 100%
```

**แจ้ง Telegram ทันที** เมื่อพบปัญหา (cooldown 10 นาที/ประเภท)
**รายงานสรุปทุก 30 นาที**

---

## 📟 12. Telegram Menu

```
┌─────────────────────────────────┐
│  📊 พอร์ต   💰 กำไรวันนี้  📓 Flip Stats │
│  🛡️ SAFE    💸 PROFIT      🔄 NORMAL    │
│  ⏸️ หยุด    ▶️ รันต่อ      💥 ปิด Position│
│  🔄 Restart              🛑 Stop Bot   │
└─────────────────────────────────┘
```

| ปุ่ม | ผลลัพธ์ |
|---|---|
| `📊 พอร์ต` | Dashboard + Rebate Saved + Regime + Trade OBI + Whale Signal |
| `💰 กำไรวันนี้` | Income History |
| `📓 Flip Stats` | Win Rate แยก Regime (L8) |
| `🛡️ SAFE` | ถอยระยะ +50–200%, ลด TP |
| `💸 PROFIT` | ลด TP 50% ปิดงานเร็ว |
| `🔄 NORMAL` | บอทตัดสินใจตามตลาดเอง + Reset Equity Kill |
| `⏸️ หยุด` | Pause bot |
| `▶️ รันต่อ` | Resume + Reset Equity Kill |
| `💥 ปิด Position` | Full Close ทันที |
| `🔄 Restart` | pm2 restart |
| `🛑 Stop Bot` | pm2 stop |

---

## 📊 13. Dashboard ตัวอย่าง

```
🏰 COMMANDER DASHBOARD v2.0
━━━━━━━━━━━━━━━━━━
📡 Status: 🟢 ONLINE | Mode: 🛡️ SAFE
💰 Balance: $420.22 ($163.03 avail)
⚙️ Grid Step: 0.870% | TP: +2.63% (Net)
━━━━━━━━━━━━━━━━━━
🚀 POSITION: 📈 LONG | 0.060 BTC
├ ❄️ PNL: $-89.02 (-32.81%)
├ Entry: $67,822.99
├ Mark:  $66,352.40
└ Net BE: $68,051.90
━━━━━━━━━━━━━━━━━━
🔮 ACTION PLAN: (DCA Strategy)
├ Layer: 8/12  ← Inventory TP Scaling active (TP ×65%)
├ Buy Next: $65,167.1
└ Predicted Avg: $67,663.4
━━━━━━━━━━━━━━━━━━
🧭 Regime: ↔️ CHOPPY (0.0234% range) | Trade OBI: -0.29
━━━━━━━━━━━━━━━━━━
💰 Maker Rebate Saved: $0.4348 (7 trades, 8.4h)
━━━━━━━━━━━━━━━━━━
🐳 Whale Signal: 🐳 STRONG BUY | OBIflat +0.72 OBIdeep +0.71 OFI +0.15
```

---

## 🚀 14. การเริ่มต้นใช้งาน

### 14.1 Infrastructure (Local Windows)

| Field | Value |
|---|---|
| Platform | Windows 11 (Local) |
| Process Manager | PM2 v6.0.8 |
| Python | venv |

### 14.2 ติดตั้ง
```bash
pip install -r requirements.txt
```

### 14.3 ตั้งค่า `.env`
```env
GL_API_KEY=your_binance_api_key
GL_API_SECRET=your_binance_api_secret
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
BINANCE_PROXY=http://user:pass@host:port   # optional

# Cloudflare Worker Proxy (เปิดเมื่อ IP ถูก block)
CF_WORKER_URL=https://binance-proxy.regency2919.workers.dev/proxy
CF_PROXY_SECRET=commander_proxy_secret_2026
```

### 14.4 รันบอท
```bash
# รันบอทหลัก
pm2 start main_commander.py --interpreter python --name commander-bot

# รัน Watchdog (แยก process)
pm2 start watchdog.py --interpreter python --name watchdog

pm2 save
pm2 list
```

### 14.5 คำสั่งที่ใช้บ่อย
```bash
pm2 logs commander-bot --lines 50
pm2 restart commander-bot
pm2 restart watchdog
pm2 stop all
```

---

## 🔬 15. หลักการ Microstructure ที่บอทใช้

| แนวคิด | การนำมาใช้ |
|---|---|
| **UQ (Unbalanced Quoting)** | `_obi_score` + `_obi_deep` — ตรวจความไม่สมดุลเทียมใน Order Book |
| **AC (Abnormal Cancellation)** | Spoof Detector Gate 3 (Quote Life Span < 3s = Spoof) |
| **UQ > AC** | OBI เป็น gate หลัก, Spoof เป็น filter รอง — ตรงกับงานวิจัย HFT |
| **Distance-Weighted OBI** | Spoofer วาง Wall ลึกเพื่อหลีก Execution risk → ถูก under-weight อัตโนมัติ |
| **Adverse Selection** | OBI Quote Cancellation — ออกก่อนถูก fill ในจังหวะเสียเปรียบ |
| **Quote Life Span (QLS)** | Gate 3: wall < 3s = Spoof (Spoof order อายุสั้นมาก) |
| **Liquidity Vacuum** | 4-Gate OBI Flip → Kill Switch |
| **Executed Flow** | OBI^T: executed trades ไม่ถูก Spoof |
| **Flickering Liquidity** | Choppy market OBI พลิกไว → Gate 4 กรอง |
| **Circuit Breaker** | Dynamic Cooldown ปรับตาม Regime |
| **Regime Anchor** | OFI 30-tick buffer ตรึงสภาวะ |
| **Triangular Loss Growth** | Dynamic Lot Sizing ลด 30% ที่ Layer 10+ |
| **Volatility/Drift Ratio** | σ/μ Gate ตรวจ Trending Real-time |
| **Variance Age Risk** | Position ถือนานขึ้น → Variance เพิ่ม → Age Exit |
| **Inventory Management** | Layer 8-9 TP Scaling — ออกเร็วแต่ยังคุ้ม fee |
| **Maker-Taker Model** | GTX Post-Only + BNB Burn = fee ต่ำสุด + Rebate Tracking |
| **Heartbeat Protocol** | `logs/heartbeat.txt` — Engine monitoring ไม่ขึ้นกับ log rotation |
| **State-based Monitoring** | Watchdog ตรวจเฉพาะเมื่อมี event จริง (ไม่ inference จาก silence) |

---

## 📝 16. Changelog

### v2.4 (2026-04-02) — Passive Market Making Features + Watchdog v1.2

**Maker Rebate Tracking**
- บันทึก `rebate = (taker_fee - maker_fee) × notional` ทุก GTX trade
- สะสมใน `_rebate_saved_total` ตลอด session
- แสดงใน Dashboard: `💰 Maker Rebate Saved: $X.XXXX (N trades, Xh)`

**OBI Quote Cancellation (Adverse Selection Guard)**
- หลังวาง GTX: monitor `_obi_score` ทุก loop ใน window 3–30 วินาที
- `OBI < -0.45` → `cancel_all_open_orders` ทันที + แจ้ง Telegram
- ป้องกัน fill ในจังหวะที่ตลาดพลิกทันที (Adverse Selection)

**Inventory TP Scaling (Layer 8-9)**
- Layer 8-9: `TP = max(fee_floor × 1.3, TP_original × 0.65)`
- ออกเร็วขึ้น ~35% แต่ยังได้กำไรสุทธิ 30% เหนือค่า fee
- Layer อื่น: ไม่แตะ TP เดิม

**Watchdog v1.2 — ไม่มโน**
- เพิ่มเป็น 14 checks (เพิ่ม Maker Rebate, OBI Cancel, Inventory TP)
- แก้ทุก false positive: ใช้ timestamp แทนการนับบรรทัด, keyword specific, unknown = pass
- `check_engine_alive` อ่านจาก **Heartbeat File** (`logs/heartbeat.txt`) แทน log parsing — แม่นยำ 100% ไม่ขึ้นกับ log rotation

**Heartbeat File Mechanism**
- บอทหลักเขียน unix timestamp ลง `logs/heartbeat.txt` ทุก Tick (~5s)
- Watchdog อ่านไฟล์โดยตรง → elapsed > 120s = crash alert
- สอดคล้องกับ Heartbeat Protocol มาตรฐาน Distributed Systems

---

### v2.3 (2026-04-02) — Bug Fixes & Watchdog v1.0

**Bug Fix: Multi-Open on Restart/KS Release**
- `Startup Guard`: รอ account cache พร้อมก่อน loop แรก (retry 10 ครั้ง)
- `Session Recovery`: `last_buy_price = entry_p` (ไม่ใช่ `cur_p`)
- `KS Release`: Restore `last_buy_price = entry_p` + Cooldown 30s
- `Circular Layer Fix`: คำนวณ Layer จาก `base_lot_u` ป้องกัน Layer บวม

**BNB Fee Burn (Auto)**
- `get_bnb_burn_status()` / `set_bnb_burn()` ใน `async_client.py`
- `taker_fee = 0.000375` / `maker_fee = 0.00015`

**Watchdog Bot v1.0**
- 11 checks ทุก 30 วินาที รันเป็น pm2 process แยก

---

### v2.2 (2026-03-29) — Cloudflare Worker Proxy (IP Bypass)
- Forward request ผ่าน CF edge → Binance API
- Auth: `X-Proxy-Secret` header | Free tier: 100,000 req/day

---

### v2.1 (2026-03-29) — Crisis Management & Institutional Risk Controls
- Dynamic Lot Sizing (100%→75%→50%→30%)
- VolatilityGate (σ/μ Real-time, Re-arm 3 bars)
- Equity Kill Switch (Drawdown > 30%)
- CRITICAL SAFE Mode (Layer 10+, Grid ×3.0)
- Variance Age Exit (72h)
- Max Daily Loss ($50/วัน)
- Rapid-Fire Order Throttle (3 orders/60s)

---

### v2.0 (2026-03-27) — Institutional-Grade Foundation
- Trade-based OBI (OBI^T) — aggTrade WebSocket Rolling 30s
- Active Regime-Aware Kill Switch (CHOPPY/TRENDING/VOLATILE)
- Outbound Token Bucket (Telegram Rate Limiter)
- Flip Logger Self-Learning (maxlen=100)
- OBI Deep Distance-Weighted (Top 5 levels)
- LOB Event-based Sync
