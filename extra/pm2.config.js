// PM2 Config — COMMANDER BOT
// รัน: pm2 start extra/pm2.config.js
module.exports = {
  apps: [
    {
      name: "commander",
      script: "main_commander.py",
      interpreter: "./venv/bin/python3",
      cwd: "/home/ubuntu/bot",
      restart_delay: 5000,       // รอ 5 วินาทีก่อน restart
      max_restarts: 10,           // restart สูงสุด 10 ครั้ง
      min_uptime: "30s",          // ถ้าตายก่อน 30s ไม่นับว่า stable
      log_file: "logs/pm2_combined.log",
      out_file: "logs/pm2_out.log",
      error_file: "logs/pm2_err.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      env: {
        PYTHONUNBUFFERED: "1",    // log ออก realtime ไม่ buffer
      },
    },
    {
      name: "watchdog",
      script: "watchdog.py",
      interpreter: "./venv/bin/python3",
      cwd: "/home/ubuntu/bot",
      restart_delay: 10000,
      max_restarts: 5,
      min_uptime: "30s",
      log_file: "logs/pm2_watchdog.log",
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
