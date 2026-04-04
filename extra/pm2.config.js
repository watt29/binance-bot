// PM2 Config — COMMANDER BOT (Windows Local)
// รัน: pm2 start extra/pm2.config.js
module.exports = {
  apps: [
    {
      name: "commander",
      script: "main_commander.py",
      interpreter: "venv\\Scripts\\python.exe",
      cwd: "C:\\Users\\Asus\\Desktop\\binance-sever-render",
      restart_delay: 5000,
      max_restarts: 10,
      min_uptime: "30s",
      log_file: "logs/pm2_combined.log",
      out_file: "logs/pm2_out.log",
      error_file: "logs/pm2_err.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
    {
      name: "watchdog",
      script: "watchdog.py",
      interpreter: "venv\\Scripts\\python.exe",
      cwd: "C:\\Users\\Asus\\Desktop\\binance-sever-render",
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
