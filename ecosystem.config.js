module.exports = {
  apps: [
    {
      name: "pm2-mcp",
      script: "/home/ted/repos/personal/pm2-mcp/.venv/bin/python3",
      args: ["server.py"],
      cwd: "/home/ted/repos/personal/pm2-mcp",
      interpreter: "none",

      // Empty ON PURPOSE — this server requires nothing from the environment.
      // The only two variables the source reads are MCP_HOST and MCP_PORT.
      // Neither is set in the running process today, so the bind address comes
      // from the defaults in server.py's __main__ block: 127.0.0.1:8486.
      //
      // `args` carried "--host 127.0.0.1 --port 8486" until 2026-09-09, and the
      // comment here claimed they were "supplied explicitly ... which take
      // precedence". That was false: server.py has no argparse and never reads
      // sys.argv, so both flags were inert and the process bound 8486 only
      // because that is the hardcoded default. The two agreed by coincidence,
      // which is why nobody noticed — editing --port here would have changed
      // nothing at all. The dead flags are removed rather than left as
      // documentation of an intent the code does not implement. To pin the bind
      // explicitly, set MCP_HOST/MCP_PORT in this block; see vikunja#770.
      //
      // Stated explicitly rather than omitted so that "no env block" can no
      // longer be read two ways. A declaration silent about env is
      // indistinguishable from one where the env was lost, and that ambiguity
      // is what made this app unsafe to `pm2 delete` and re-create even though
      // it was already declared here.
      //
      // Do NOT add a shared-secrets loader. The running process carries none
      // of those keys today; adding them would push credentials into a process
      // that reads none of them, and PM2 would write every one into its dump
      // file at the next `pm2 save`.
      //
      // Note this does not scrub inheritance: PM2 always passes the parent
      // environment through, so a `pm2 start` from an interactive shell still
      // inherits whatever that shell sourced. What this block asserts is what
      // the app *requires*, which is nothing.
      env: {},

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
