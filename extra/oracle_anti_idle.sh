#!/bin/bash
# ============================================================
# COMMANDER BOT — Oracle Cloud Anti-Idle Script
# ป้องกัน Oracle ยึดเครื่อง Always Free คืน
# เงื่อนไขที่ Oracle ใช้ตัดสิน "Idle":
#   CPU < 20% (p95), Network < 20%, RAM < 20% (ARM เท่านั้น)
# ============================================================
# ติดตั้ง: bash oracle_anti_idle.sh
# ============================================================

set -e
echo "======================================"
echo " Oracle Anti-Idle Setup"
echo "======================================"

# --- 1. ติดตั้ง stress-ng ---
echo "[1/3] Installing stress-ng..."
sudo apt-get install -y stress-ng

# --- 2. สร้าง anti-idle script ---
echo "[2/3] Creating anti-idle script..."
cat > ~/anti_idle.sh << 'EOF'
#!/bin/bash
# รัน stress-ng เบาๆ 5 นาทีทุกชั่วโมง
# เพื่อให้ CPU และ RAM เกิน 20% threshold ของ Oracle

LOG="/home/ubuntu/bot/logs/anti_idle.log"
DATE=$(date '+%Y-%m-%d %H:%M:%S')

echo "[$DATE] Running anti-idle stress..." >> "$LOG"

# CPU stress 2 workers, 30% load, นาน 5 นาที
stress-ng --cpu 2 --cpu-load 30 --timeout 300s --quiet

# RAM stress 512MB, นาน 2 นาที
stress-ng --vm 1 --vm-bytes 512M --timeout 120s --quiet

echo "[$DATE] Anti-idle done." >> "$LOG"
EOF

chmod +x ~/anti_idle.sh

# --- 3. ตั้ง Cron job รันทุกชั่วโมง ---
echo "[3/3] Setting up cron job..."
# รันทุกชั่วโมงที่นาทีที่ 30 (เพื่อไม่ให้ตรงกับ bot check)
(crontab -l 2>/dev/null; echo "30 * * * * /home/ubuntu/anti_idle.sh") | crontab -

echo ""
echo "======================================"
echo " Anti-Idle Setup เสร็จแล้ว!"
echo "======================================"
echo " Cron schedule: ทุกชั่วโมงที่นาทีที่ 30"
echo " Log: ~/bot/logs/anti_idle.log"
echo ""
echo " ตรวจสอบ cron:"
echo "   crontab -l"
echo ""
echo " ทดสอบ manual:"
echo "   bash ~/anti_idle.sh"
echo "======================================"
