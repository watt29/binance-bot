import os
from dotenv import load_dotenv  # pyre-ignore

# โหลดไฟล์ .env จาก root ของโปรเจกต์
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, '.env')
load_dotenv(ENV_PATH)

# --- [TELEGRAM CONFIG] ---
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# --- [PATHS] ---
DB_PATH  = os.path.join(BASE_DIR, 'data', 'trading_data.db')
LOG_PATH = os.path.join(BASE_DIR, 'logs', 'bot_errors.log')

# --- [BINANCE GLOBAL API ONLY] ---
GL_API_KEY    = os.getenv("GL_API_KEY", "").strip()
GL_API_SECRET = os.getenv("GL_API_SECRET", "").strip()
BINANCE_PROXY = os.getenv("BINANCE_PROXY", "").strip() or None

# --- DEBUG (ตรวจสอบความพร้อม) ---
if __name__ == "__main__":
    print(f"--- Config Check ---")
    print(f"GL API Key : {'✅ READY' if GL_API_KEY else '❌ MISSING'}") 
    print(f"GL Secret  : {'✅ READY' if GL_API_SECRET else '❌ MISSING'}")
    print(f"Telegram   : {'✅ READY' if TELEGRAM_TOKEN else '❌ MISSING'}")
    print(f"ENV Path   : {ENV_PATH}")
