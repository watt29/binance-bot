# Migration Checklist — Google Cloud → Oracle Cloud Tokyo

> ## ⚠️ สถานะปัจจุบัน (2026-04-04)
> **หยุดพักแผน Migration ชั่วคราว — รันบอทบนเครื่อง Windows ตัวเองแทน**
> เหตุผล: ยังไม่มีบัตรเครดิต/วิธีชำระเงินที่พร้อม
> กลับมาทำต่อเมื่อพร้อม — ใช้ Vultr + PayPal
>
> **วิธีรันบนเครื่อง Windows ตอนนี้:**
> ```
> cd C:\Users\Asus\Desktop\binance-sever-render
> pm2 start extra\pm2.config.js
> pm2 save
> ```

## PHASE 1: สมัคร Oracle Cloud (PAYG — แนะนำอย่างยิ่ง)
- [ ] ไปที่ cloud.oracle.com → สมัครบัญชีใหม่
- [ ] เลือกบัญชีแบบ **Pay As You Go (PAYG)**
  - ใส่บัตรเครดิต (ไม่แนะนำบัตรเดบิตหรือบัตรเสมือน — มักถูกปฏิเสธ)
  - Oracle จะ **Hold วงเงิน ~$100 (~3,500 บาท)** เพื่อยืนยันตัวตน (คืนภายใน 1-2 สัปดาห์)
  - ยังคงใช้ฟรีตราบใดที่ไม่เกิน Free Tier limit (4 OCPU / 24GB RAM)
  - **ข้อดี PAYG:** ไม่ถูกยึดเครื่องคืนเพราะ Idle + ได้ทรัพยากรง่ายกว่าบัญชีฟรี
- [ ] เลือก Home Region: **Japan East (Tokyo)**

## PHASE 2: สร้าง VM Instance
- [ ] ไปที่ Compute → Instances → Create Instance
- [ ] ตั้งชื่อ: `commander-bot`
- [ ] Image: **Ubuntu 22.04 LTS**
- [ ] Shape: **VM.Standard.A1.Flex**
  - OCPU: **2** (พอสำหรับ 1 bot, เผื่อขยายได้ถึง 4)
  - RAM: **12 GB** (เผื่อขยายได้ถึง 24)
- [ ] เปิด **Assign Public IPv4**
- [ ] Download SSH Key (.pem) เก็บไว้
- [ ] จด **Public IP** ไว้ใช้ทำ Whitelist ใน Binance

## PHASE 3: ตั้งค่า Firewall ชั้นที่ 1 — VCN Security List (Oracle Dashboard)
> Networking → Virtual Cloud Networks → [VCN ของคุณ] → Security Lists → Default Security List

เพิ่ม **Ingress Rules** ทั้งหมดนี้:

| Port | Protocol | Source CIDR | หมายเหตุ |
|------|----------|-------------|---------|
| 22   | TCP | `0.0.0.0/0` | SSH |
| 80   | TCP | `0.0.0.0/0` | HTTP |
| 443  | TCP | `0.0.0.0/0` | HTTPS / Telegram API |
| 8080 | TCP | `0.0.0.0/0` | Bot health check |
| ALL  | ALL | `10.0.0.0/16` | Internal VCN traffic |

## PHASE 4: ตั้งค่า Firewall ชั้นที่ 2 — Network Security Group (ถ้ามี)
- [ ] ตรวจสอบว่า Instance มี NSG ผูกอยู่ไหม (Compute → Instance Details → Network Security Groups)
- [ ] ถ้ามี NSG → เพิ่ม Ingress Rules เหมือน Phase 3 ทุก port ใน NSG นั้นด้วย
- [ ] ถ้าไม่มี NSG → ข้ามขั้นตอนนี้ได้เลย

## PHASE 5: SSH เข้า Instance
```bash
chmod 400 oracle_key.pem
ssh -i oracle_key.pem ubuntu@ORACLE_PUBLIC_IP
```

## PHASE 6: อัปโหลดโค้ด (จาก Windows)
```bash
# รันบน Windows (Git Bash / PowerShell)
scp -i oracle_key.pem -r /c/Users/Asus/Desktop/binance-sever-render ubuntu@ORACLE_IP:~/bot
```

## PHASE 7: ติดตั้ง + ตั้งค่า Firewall ชั้นที่ 3 — OS iptables (auto ใน script)
```bash
# SSH เข้าไปแล้วรัน
bash ~/bot/extra/oracle_setup.sh
```
Script จะเปิด iptables port 22, 80, 443, 8080 และ save ให้อัตโนมัติ

## PHASE 8: ตั้งค่า .env
```bash
nano ~/bot/.env
```
```
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
GL_API_KEY=
GL_API_SECRET=
CF_WORKER_URL=
CF_PROXY_SECRET=
```

## PHASE 9: Whitelist IP ใน Binance API Key
- [ ] เข้า Binance → API Management → Edit API Key
- [ ] เปิด "Restrict access to trusted IPs only"
- [ ] ใส่ Public IP ของ Oracle instance
- [ ] Save → รอ 10 นาทีให้ effect

## PHASE 10: ทดสอบก่อน Start PM2
```bash
cd ~/bot
source venv/bin/activate
python shared/config.py          # ตรวจ config — ต้องเห็น ✅ ทุกข้อ
python main_commander.py         # ทดสอบ manual — ดู log ไม่มี error
# Ctrl+C เพื่อหยุด
```

## PHASE 11: Start ด้วย PM2
```bash
pm2 start ~/bot/extra/pm2.config.js
pm2 save
pm2 startup    # copy คำสั่งที่ PM2 แสดง แล้วรันอีกครั้ง
```

## PHASE 12: ตรวจสอบ
- [ ] `pm2 status` — commander และ watchdog สถานะ **online**
- [ ] `pm2 logs commander` — ไม่มี error
- [ ] Telegram ส่ง /status — บอทตอบกลับปกติ
- [ ] `pm2 logs watchdog` — watchdog เห็น heartbeat

## PHASE 13: ติดตั้ง Anti-Idle (สำหรับบัญชีฟรีที่ยังไม่ได้ PAYG)
> ถ้าใช้ PAYG แล้ว — ข้ามขั้นตอนนี้ได้เลย

Oracle ยึดเครื่องคืนถ้า CPU < 20% / Network < 20% / RAM < 20% ต่อเนื่อง
```bash
bash ~/bot/extra/oracle_anti_idle.sh
```
- [ ] `crontab -l` — เห็น anti_idle.sh อยู่ใน cron
- [ ] `bash ~/anti_idle.sh` — ทดสอบ manual รันได้ปกติ

**กฎที่ต้องระวัง (AUP):**
- ห้ามขุด crypto — โดนแบนทันที
- ห้ามใช้เป็น proxy/VPN ผิดกฎหมาย
- ตรวจสอบบัตรเครดิตที่ผูกอยู่ให้ยังใช้ได้เสมอ
- แนะนำติดตั้ง `fail2ban` เพิ่มความปลอดภัย

## PHASE 14: ปิด Google Cloud (หลังแน่ใจแล้ว)
- [ ] รอให้บอทรัน stable บน Oracle อย่างน้อย **24 ชั่วโมง**
- [ ] Stop instance บน Google Cloud (อย่าเพิ่ง Delete — เผื่อ rollback)
- [ ] Delete instance หลังจาก 3-7 วัน ถ้าไม่มีปัญหา
