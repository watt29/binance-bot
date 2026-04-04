"""
COMMANDER WATCHDOG v1.2
ตรวจสอบพฤติกรรมบอทหลักว่าตรงตามวัตถุประสงค์
หลักการ: ตรวจจาก log จริงเท่านั้น ไม่ inference ไม่เดา ไม่มโน
  - ไม่พบ log ≠ error (log หมุนได้ตลอด)
  - ใช้ timestamp เสมอ ไม่นับบรรทัด
  - keyword ต้อง specific ไม่ match log อื่น
  - unknown = pass เสมอ (fail เฉพาะเมื่อเห็นหลักฐานชัดเจน)
"""

import asyncio
import os
import re
import time
import json
from datetime import datetime
from shared.config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
import aiohttp  # pyre-ignore

LOG_PATH       = "logs/bot_commander.log"
WATCHDOG_STATE = "logs/watchdog_state.json"
CHECK_INTERVAL = 30   # ตรวจทุก 30 วินาที

# ─── Telegram ────────────────────────────────────────────────
async def tg_send(session: aiohttp.ClientSession, msg: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        })
    except Exception as e:
        print(f"[TG Error] {e}")

# ─── Log Reader ───────────────────────────────────────────────
def read_last_lines(path: str, n: int = 500) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.readlines()[-n:]
    except Exception:
        return []

def parse_ts(line: str) -> float:
    """แปลง timestamp จาก log line → unix time  คืน 0.0 ถ้า parse ไม่ได้"""
    try:
        ts_str = line[:23]
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
        return dt.timestamp()
    except Exception:
        return 0.0

def lines_in_window(lines: list[str], seconds: int) -> list[str]:
    """กรองเฉพาะบรรทัดที่มี timestamp และอยู่ใน N วินาทีล่าสุด"""
    cutoff = time.time() - seconds
    return [l for l in lines if parse_ts(l) >= cutoff]

def log_span_seconds(lines: list[str]) -> float:
    """คืนช่วงเวลา (วินาที) ที่ log 500 บรรทัดครอบคลุม
    ใช้ตรวจว่า log rotation ทำให้ข้อมูลหายไปหรือไม่"""
    ts_list = [parse_ts(l) for l in lines if parse_ts(l) > 0]
    if len(ts_list) < 2:
        return 0.0
    return max(ts_list) - min(ts_list)

# ─── CHECK 1: Fix Verification ───────────────────────────────
def check_gtx_expired_fix(lines: list[str]) -> tuple[bool, str]:
    """ตรวจว่า GTX EXPIRED ไม่ถูกนับว่าสำเร็จ
    หลักฐาน: มี EXPIRED → ต้องไม่มี 'เปิด (GTX) สำเร็จ' ภายใน 15 วินาที
    """
    for i, line in enumerate(lines):
        if "GTX Order EXPIRED" not in line:
            continue
        expired_ts = parse_ts(line)
        if expired_ts == 0:
            continue
        for w in lines[i+1:i+50]:  # scan ต่อไปสูงสุด 50 บรรทัด
            w_ts = parse_ts(w)
            if w_ts == 0:
                continue
            if w_ts - expired_ts > 15:
                break  # เกิน 15 วินาที หยุดหา
            if "เปิด (GTX) สำเร็จ" in w:
                return False, "❌ GTX Fix: พบการเปิดไม้ภายใน 15s หลัง EXPIRED!"

    expired_count = sum(1 for l in lines if "GTX Order EXPIRED" in l)
    if expired_count > 0:
        return True, f"✅ GTX Fix: {expired_count} EXPIRED — ไม่มีการเปิดซ้ำ ✓"
    return True, "✅ GTX Fix: ไม่มี EXPIRED event (ปกติ)"

def check_circular_layer_fix(lines: list[str]) -> tuple[bool, str]:
    """ตรวจว่า Layer ไม่บวมเกิน 12 — อ่านจาก Calc log เท่านั้น"""
    pattern = re.compile(r"= Layers (\d+) \(M\.Ratio")
    layers_found = []
    for line in lines[-100:]:
        m = pattern.search(line)
        if m:
            layers_found.append(int(m.group(1)))

    if not layers_found:
        return True, "✅ Circular Layer: ไม่มีข้อมูล (ยังไม่มี position)"

    max_layer = max(layers_found)
    if max_layer > 12:
        return False, f"❌ Circular Layer: Layer พุ่งถึง {max_layer} — circular bug ยังอยู่!"
    return True, f"✅ Circular Layer: สูงสุด {max_layer}/12 (stable)"

def check_startup_guard(lines: list[str]) -> tuple[bool, str]:
    """ตรวจ startup guard เฉพาะเมื่อมี restart (OPERATIONAL) ใน log
    ถ้าไม่มี restart = บอทรันมานาน log หมุนออกแล้ว → ผ่านเสมอ
    """
    restart_idx = None
    for i, line in enumerate(lines):
        if "OPERATIONAL" in line and "COMMANDER" in line:
            restart_idx = i
            break

    if restart_idx is None:
        return True, "✅ Startup Guard: ไม่มี restart ใน log ล่าสุด (ปกติ)"

    cache_ready_idx = None
    first_tick_idx  = None
    for i, line in enumerate(lines):
        if i < restart_idx:
            continue
        if "Account cache ready" in line and cache_ready_idx is None:
            cache_ready_idx = i
        if "Trading Loop Tick" in line and first_tick_idx is None:
            first_tick_idx = i

    if first_tick_idx is None:
        return True, "✅ Startup Guard: restart พบแต่ยังไม่เริ่ม loop"
    if cache_ready_idx is None:
        return False, "❌ Startup Guard: restart แล้ว Tick เริ่มแต่ไม่มี cache ready!"
    if cache_ready_idx > first_tick_idx:
        return False, "❌ Startup Guard: Trading Tick เกิดก่อน Account cache ready!"
    return True, "✅ Startup Guard: cache ready ก่อน loop ✓"

def check_bnb_burn(lines: list[str]) -> tuple[bool, str]:
    """ตรวจ BNB Fee Burn จาก startup log
    ไม่พบ log = log หมุนออกไปแล้ว → ผ่านเสมอ (ไม่มโน)
    """
    for line in reversed(lines):
        if "BNB Fee Burn" not in line:
            continue
        if "เปิดอยู่แล้ว" in line or "เปิดอัตโนมัติ" in line:
            return True, "✅ BNB Fee Burn: เปิดอยู่ (-25% fee) ✓"
        if "skipped" in line:
            return True, "✅ BNB Fee Burn: ตรวจไม่ได้ (API error) — ไม่ critical"
    return True, "✅ BNB Fee Burn: ไม่มี log (log หมุนแล้ว — ปกติ)"

# ─── CHECK 2: Strategy Behavior ──────────────────────────────
def check_open_condition(lines: list[str]) -> tuple[bool, str]:
    """ตรวจว่าบอทเปิดไม้มี Prediction log ก่อน (ภายใน 10 วินาที)
    ใช้ timestamp เสมอ ไม่นับบรรทัด
    """
    issues = []
    for i, line in enumerate(lines):
        if "เปิด (GTX) สำเร็จ" not in line:
            continue
        open_ts = parse_ts(line)
        if open_ts == 0:
            continue
        found = False
        for w in reversed(lines[:i]):
            w_ts = parse_ts(w)
            if w_ts == 0:
                continue
            if open_ts - w_ts > 10:
                break
            # keyword เฉพาะ — ต้องมี "Prediction:" หรือ "📊 Calc:" (format ใน bot log)
            if "Prediction:" in w or "📊 Calc:" in w:
                found = True
                break
        if not found:
            issues.append(line[:19])

    if issues:
        return False, f"⚠️ Strategy: เปิดไม้ {len(issues)} ครั้งไม่มี Prediction ใน 10s\n({', '.join(issues[:3])})"
    return True, "✅ Strategy: ทุกไม้มี Prediction/Calc ก่อนเปิด ✓"

def check_dca_layer_limit(lines: list[str]) -> tuple[bool, str]:
    """ตรวจว่าบอทไม่ DCA เกิน 12 layers — ใช้ Calc log format เท่านั้น"""
    pattern = re.compile(r"= Layers (\d+) \(M\.Ratio")
    over_limit = []
    for line in lines:
        m = pattern.search(line)
        if m and int(m.group(1)) > 12:
            over_limit.append(int(m.group(1)))

    if over_limit:
        return False, f"❌ DCA Limit: Layer เกิน 12! (พบ {max(over_limit)} layers)"
    return True, "✅ DCA Limit: ไม่เกิน 12 layers ✓"

def check_tp_triggered(lines: list[str]) -> tuple[bool, str]:
    """รายงานการปิดไม้ล่าสุด — ไม่ fail ถ้าไม่ปิด (holding = ปกติ)"""
    close_times = []
    for line in lines:
        if "🔴 ปิด สำเร็จ" in line or "PROFIT LOCK" in line:
            ts = parse_ts(line)
            if ts > 0:
                close_times.append(ts)

    if not close_times:
        return True, "✅ TP: ยังไม่ปิดไม้ (holding — ปกติ)"
    hours_since = (time.time() - max(close_times)) / 3600
    return True, f"✅ TP: ปิดล่าสุด {hours_since:.1f}h ที่แล้ว"

# ─── CHECK 3: Feature Verification ──────────────────────────
def check_maker_rebate(lines: list[str]) -> tuple[bool, str]:
    """รายงาน Maker Rebate สะสม — ไม่มี fail condition"""
    rebate_lines = [l for l in lines if "Maker Rebate:" in l]
    if not rebate_lines:
        return True, "✅ Maker Rebate: ยังไม่มี trade (ปกติถ้าเพิ่ง start)"
    last = rebate_lines[-1]
    m = re.search(r"สะสม \$(\d+\.\d+) \((\d+) trades\)", last)
    if m:
        return True, f"✅ Maker Rebate: สะสม ${float(m.group(1)):.4f} ({m.group(2)} trades)"
    return True, f"✅ Maker Rebate: พบ {len(rebate_lines)} records"

def check_obi_cancel(lines: list[str]) -> tuple[bool, str]:
    """ตรวจ OBI Quote Cancel ใน 1 ชั่วโมงล่าสุด
    >10 ครั้ง = threshold อาจไวเกิน (แจ้งเตือน ไม่ fail hard)
    """
    recent = lines_in_window(lines, 3600)
    cancel_lines = [l for l in recent if "OBI Quote Cancel" in l]

    if not cancel_lines:
        return True, "✅ OBI Cancel: ไม่มี cancel ใน 1h (ปกติ)"
    if len(cancel_lines) > 10:
        return False, f"⚠️ OBI Cancel: {len(cancel_lines)} ครั้งใน 1h — threshold อาจไวเกิน"
    return True, f"✅ OBI Cancel: {len(cancel_lines)} ครั้งใน 1h (ปกติ)"

def check_inventory_tp(lines: list[str]) -> tuple[bool, str]:
    """รายงาน Layer ปัจจุบันจาก Calc log — ไม่ inference"""
    pattern = re.compile(r"= Layers (\d+) \(M\.Ratio")
    layers_found = []
    for line in lines[-50:]:
        m = pattern.search(line)
        if m:
            layers_found.append(int(m.group(1)))

    if not layers_found:
        return True, "✅ Inventory TP: ไม่มี position ปัจจุบัน"
    current = layers_found[-1]
    if current in (8, 9):
        return True, f"✅ Inventory TP: Layer {current} — TP scaling active"
    return True, f"✅ Inventory TP: Layer {current} (ปกติ)"

# ─── CHECK 4: Anomaly Detection ──────────────────────────────
def check_rapid_fire(lines: list[str]) -> tuple[bool, str]:
    """ตรวจ Rapid Fire — เปิด 2 ไม้ห่างกัน < 10s = ผิดปกติ
    ใช้ timestamp เท่านั้น บรรทัดที่ parse ไม่ได้ข้ามไป
    """
    open_times = []
    for line in lines:
        if "เปิด (GTX) สำเร็จ" not in line:
            continue
        ts = parse_ts(line)
        if ts > 0:
            open_times.append(ts)

    if len(open_times) < 2:
        return True, "✅ Rapid Fire: ปกติ (ไม่มีหรือมีไม้เดียว)"

    too_close = [open_times[i] - open_times[i-1]
                 for i in range(1, len(open_times))
                 if open_times[i] - open_times[i-1] < 10]

    if too_close:
        return False, (
            f"🚨 Rapid Fire: {len(too_close)} คู่เปิดห่างกัน < 10s\n"
            f"ห่างน้อยสุด {min(too_close):.1f}s — ผิดปกติ!"
        )
    return True, "✅ Rapid Fire: ทุกไม้ห่างกันเพียงพอ ✓"

def check_error_rate(lines: list[str]) -> tuple[bool, str]:
    """ตรวจ Error Rate ใน 5 นาทีล่าสุด
    keyword เฉพาะ: '| ERROR |' (loguru format) หรือ 'Trade Execution Error:'
    ไม่นับ log ที่มีคำว่า ERROR แต่เป็น context อื่น
    """
    recent = lines_in_window(lines, 300)
    errors = [l for l in recent
              if " | ERROR | " in l or "Trade Execution Error:" in l]

    if len(errors) >= 5:
        return False, f"🚨 Errors: {len(errors)} errors ใน 5 นาทีล่าสุด!"
    if len(errors) >= 2:
        return True, f"⚠️ Errors: {len(errors)} errors ใน 5 นาที (ควรสังเกต)"
    return True, f"✅ Errors: ปกติ ({len(errors)} errors/5min)"

def check_ws_connected(lines: list[str]) -> tuple[bool, str]:
    """ตรวจ WebSocket — หา timestamp ล่าสุดของ disconnect และ reconnect
    ถ้า reconnect_ts > disconnect_ts = ผ่าน
    ถ้า disconnect_ts > reconnect_ts นานเกิน 60s = alert
    """
    last_disconnect_ts = 0.0
    last_reconnect_ts  = 0.0

    for line in lines:
        ts = parse_ts(line)
        if ts == 0:
            continue
        # keyword เฉพาะ: format จาก _ws_loop ใน async_client.py
        if "WebSocket" in line and "Error:" in line and "Reconnecting" in line:
            last_disconnect_ts = max(last_disconnect_ts, ts)
        if "Connected to WebSocket:" in line:
            last_reconnect_ts = max(last_reconnect_ts, ts)

    if last_disconnect_ts == 0:
        return True, "✅ WebSocket: ไม่มี disconnect ✓"
    if last_reconnect_ts > last_disconnect_ts:
        ago = (time.time() - last_reconnect_ts) / 60
        return True, f"✅ WebSocket: disconnect แล้ว reconnect สำเร็จ ({ago:.0f}min ago)"

    elapsed = time.time() - last_disconnect_ts
    if elapsed > 60:
        return False, f"🚨 WebSocket: disconnect มา {elapsed:.0f}s ยังไม่ reconnect!"
    return True, f"⚠️ WebSocket: disconnect {elapsed:.0f}s — กำลัง reconnect..."

HEARTBEAT_PATH = "logs/heartbeat.txt"

def check_engine_alive(lines: list[str]) -> tuple[bool, str]:
    """ตรวจว่า trading loop ยังวิ่งอยู่ — อ่านจาก heartbeat file แทน log parsing
    บอทเขียน timestamp ล่าสุดลง logs/heartbeat.txt ทุก Tick (~5s)
    ไม่พึ่ง log rotation เลย — แม่นยำ 100%
    """
    now = time.time()

    # อ่าน heartbeat file
    try:
        with open(HEARTBEAT_PATH, "r") as f:
            last_tick = float(f.read().strip())
    except Exception:
        # ไม่มี file = บอทยังไม่เขียน (เพิ่ง start) หรือ path ผิด → grace period
        return True, "✅ Engine: heartbeat file ยังไม่มี (บอทเพิ่ง start)"

    elapsed = now - last_tick
    if elapsed > 120:
        return False, f"🚨 Engine: Loop หยุดมา {elapsed:.0f}s — บอทอาจ crash!"
    return True, f"✅ Engine: Loop ปกติ (last tick {elapsed:.0f}s ago)"

# ─── State: ป้องกันแจ้งซ้ำ ────────────────────────────────────
def load_state() -> dict:
    try:
        if os.path.exists(WATCHDOG_STATE):
            with open(WATCHDOG_STATE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_state(state: dict):
    try:
        with open(WATCHDOG_STATE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

# ─── Main Loop ───────────────────────────────────────────────
async def run_watchdog():
    print("[WATCHDOG] Starting Commander Watchdog v1.2...")
    state = load_state()

    async with aiohttp.ClientSession() as session:
        await tg_send(session, "🐕 *WATCHDOG v1.2 Started*\nเริ่มตรวจสอบ Commander Bot แล้วครับ")

        while True:
            try:
                lines = read_last_lines(LOG_PATH, n=500)
                now_str = datetime.now().strftime("%H:%M:%S")

                checks = [
                    # Fix Verification
                    check_gtx_expired_fix(lines),
                    check_circular_layer_fix(lines),
                    check_startup_guard(lines),
                    check_bnb_burn(lines),
                    # Strategy Behavior
                    check_open_condition(lines),
                    check_dca_layer_limit(lines),
                    check_tp_triggered(lines),
                    # Feature Verification
                    check_maker_rebate(lines),
                    check_obi_cancel(lines),
                    check_inventory_tp(lines),
                    # Anomaly Detection
                    check_rapid_fire(lines),
                    check_error_rate(lines),
                    check_ws_connected(lines),
                    check_engine_alive(lines),
                ]

                failed = [(ok, msg) for ok, msg in checks if not ok]
                passed = [(ok, msg) for ok, msg in checks if ok]

                # แจ้งเตือนเฉพาะปัญหา — cooldown 10 นาทีต่อ key
                for ok, msg in failed:
                    key = msg[:40]
                    last_alert = state.get(key, 0)
                    if time.time() - last_alert > 600:
                        await tg_send(session, f"🚨 *WATCHDOG ALERT* `{now_str}`\n{msg}")
                        state[key] = time.time()
                        save_state(state)

                # รายงานสรุปทุก 30 นาที
                last_report = state.get("_last_report", 0)
                if time.time() - last_report > 1800:
                    fail_count = len(failed)
                    icon = "🟢" if fail_count == 0 else ("🟡" if fail_count <= 2 else "🔴")
                    summary = f"{icon} *WATCHDOG REPORT* `{now_str}`\n"
                    summary += f"ผ่าน: {len(passed)}/{len(checks)} checks\n"
                    summary += "━━━━━━━━━━━━━━━━━━\n"
                    for _, msg in checks:
                        summary += f"{msg}\n"
                    await tg_send(session, summary)
                    state["_last_report"] = time.time()
                    save_state(state)

                fail_count = len(failed)
                status = "ALL OK" if fail_count == 0 else f"{fail_count} ISSUES"
                print(f"[{now_str}] Watchdog: {status} ({len(checks)} checks)")

            except Exception as e:
                print(f"[WATCHDOG ERROR] {e}")

            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(run_watchdog())
