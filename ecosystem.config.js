module.exports = {
  apps: [
    {
      name: "pm2-mcp",
      script: "/home/ted/repos/personal/pm2-mcp/.venv/bin/python3",
      args: ["server.py", "--host", "127.0.0.1", "--port", "8486"],
      cwd: "/home/ted/repos/personal/pm2-mcp",
      interpreter: "none",

      restart_delay: 5000,
      max_restarts: 10,
      min_uptime: "10s",

      out_file: "/home/ted/logs/pm2-mcp.log",
      error_file: "/home/ted/logs/pm2-mcp.log",
      merge_logs: true,
      time: true,
    },
  ],
};
