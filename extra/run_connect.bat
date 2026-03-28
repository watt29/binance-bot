@echo off
echo.
echo ========================================================
echo [ 🚨 คำเตือนสำคัญมาก ]
echo กรูณาปิดหน้าต่างเว็บเบราว์เซอร์ Google Chrome 
echo ทุกหน้าต่างที่เปิดอยู่บนเครื่องให้หมดก่อนกดปุ่มใดๆ !!!
echo ========================================================
echo.
echo 1. เข้าสู่ระบบ Binance TH (ประเทศไทย)
echo 2. เข้าสู่ระบบ Binance GLOBAL (สากล)
echo.
set /p choice="👉 โปรดเลือกบริการที่ต้องการ (1 หรือ 2): "

if "%choice%"=="1" (
    echo กำลังเปิดระบบ Automation เชื่อมต่อ Binance TH...
    python connect_browser.py
) else if "%choice%"=="2" (
    echo กำลังเปิดระบบ Automation เชื่อมต่อ Binance Global...
    python connect_global.py
) else (
    echo [!] เลือกไม่ถูกต้อง กรูณาเปิดไฟล์ใหม่อีกครั้ง
)

pause
