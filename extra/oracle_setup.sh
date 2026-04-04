#!/bin/bash
# ============================================================
# COMMANDER BOT — Oracle Cloud Setup Script
# Ubuntu 22.04 LTS / ARM Ampere A1 (Tokyo)
# รัน: bash oracle_setup.sh
# ============================================================

set -e
echo "======================================"
echo " COMMANDER BOT — Oracle Cloud Setup"
echo "======================================"

# --- 1. System Update ---
echo "[1/8] Updating system..."
sudo apt-get update -y && sudo apt-get upgrade -y

# --- 2. Install Python 3.11 + pip + venv ---
echo "[2/8] Installing Python 3.11..."
sudo apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip git curl screen iptables-persistent netfilter-persistent

# --- 3. Install PM2 (Node.js) ---
echo "[3/8] Installing Node.js + PM2..."
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g pm2

# --- 4. Upload/Clone repo ---
echo "[4/8] Setting up bot directory..."
# ถ้าใช้ SCP อัปโหลดมาแล้ว bot จะอยู่ที่ ~/bot แล้ว
# ถ้าใช้ GitHub ให้ uncomment บรรทัดนี้:
# git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git ~/bot

cd ~/bot

# --- 5. Setup Python venv ---
echo "[5/8] Setting up Python virtualenv..."
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# --- 6. สร้าง directories ที่จำเป็น ---
echo "[6/8] Creating directories..."
mkdir -p logs data

# ============================================================
# --- 7. OS Firewall — ชั้นที่ 3 (สำคัญที่สุด) ---
# Oracle Ubuntu มีกฎ iptables REJECT ติดมาแต่แรก
# ต้องเปิดเองทุก port ที่ต้องการ
# ============================================================
echo "[7/8] Configuring OS firewall (iptables)..."

# เปิด SSH (ป้องกันล็อกตัวเองออก)
sudo iptables -I INPUT 6 -p tcp --dport 22 -j ACCEPT

# เปิด HTTP/HTTPS (สำหรับ Telegram API outbound + health check)
sudo iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT

# เปิด port health check ของบอท
sudo iptables -I INPUT 6 -p tcp --dport 8080 -j ACCEPT

# เปิด traffic ภายใน VCN (10.0.0.0/16)
sudo iptables -I INPUT 6 -s 10.0.0.0/16 -j ACCEPT

# บันทึกให้คงอยู่หลัง reboot
sudo netfilter-persistent save

echo "    iptables rules saved."

# ============================================================
# --- 8. แสดง Public IP ---
# นำ IP นี้ไป Whitelist ใน Binance API Key settings
# ============================================================
echo "[8/8] Getting Public IP..."
PUBLIC_IP=$(curl -s ifconfig.me)
echo ""
echo "======================================"
echo " Setup เสร็จแล้ว!"
echo "======================================"
echo ""
echo " Public IP ของเครื่องนี้: $PUBLIC_IP"
echo " *** นำ IP นี้ไป Whitelist ใน Binance API Key ***"
echo " Binance > API Management > Edit > Restrict access to trusted IPs"
echo ""
echo " ขั้นตอนถัดไป:"
echo " 1. ตั้งค่า .env:"
echo "    nano ~/bot/.env"
echo ""
echo "    TELEGRAM_TOKEN=xxx"
echo "    TELEGRAM_CHAT_ID=xxx"
echo "    GL_API_KEY=xxx"
echo "    GL_API_SECRET=xxx"
echo "    CF_WORKER_URL="
echo "    CF_PROXY_SECRET="
echo ""
echo " 2. ทดสอบ config:"
echo "    source ~/bot/venv/bin/activate && python ~/bot/shared/config.py"
echo ""
echo " 3. ทดสอบ manual:"
echo "    source ~/bot/venv/bin/activate && python ~/bot/main_commander.py"
echo ""
echo " 4. Start ด้วย PM2:"
echo "    pm2 start ~/bot/extra/pm2.config.js"
echo "    pm2 save && pm2 startup"
echo ""
echo " 5. ตรวจสอบสถานะ:"
echo "    pm2 status"
echo "    pm2 logs commander"
echo "======================================"
echo ""
echo " CHECKLIST Oracle Cloud Dashboard (ทำมือ):"
echo " [ ] Networking > VCN > Security Lists > เพิ่ม Ingress TCP port 22, 80, 443, 8080"
echo " [ ] ถ้ามี NSG ผูกกับ Instance ให้เพิ่ม rule ใน NSG ด้วย"
echo " [ ] Whitelist IP: $PUBLIC_IP ใน Binance API Key"
echo "======================================"
