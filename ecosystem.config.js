module.exports = {
  apps: [
    {
      name: "pm2-mcp",
      script: "/home/ted/repos/personal/pm2-mcp/.venv/bin/python3",
      args: ["server.py", "--host", "127.0.0.1", "--port", "8486"],
      cwd: "/home/ted/repos/personal/pm2-mcp",
      interpreter: "none",

      // Empty ON PURPOSE — this server requires nothing from the environment.
      // The only two variables the source reads are MCP_HOST and MCP_PORT.
      // Neither is set in the running process today, so the bind address comes
      // from the defaults in server.py's __main__ block: 127.0.0.1:8486.
      //
      // THE FLAGS IN `args` ABOVE ARE LIVE AS OF vikunja#770 (2026-09-10).
      // Say that plainly, because for months they were NOT and the comment here
      // claimed otherwise. History, so the next reader does not re-derive it:
      //
      //   - Until 2026-09-09 `args` carried these same two flags while server.py
      //     had no argparse and never read sys.argv. Both were inert. The process
      //     bound 8486 only because that was the hardcoded default, and the two
      //     agreed by coincidence — editing --port here would have changed nothing.
      //   - On 2026-09-09 the dead flags were removed rather than left as
      //     documentation of an intent the code did not implement.
      //   - #770 added the argparse that makes them real, so they are restored.
      //
      // Precedence is argv > MCP_HOST/MCP_PORT > server.py's 127.0.0.1:8486
      // default, so these flags now win over anything set in `env` below.
      //
      // The bind is LOOPBACK-ONLY and enforced in code: a non-loopback --host
      // exits non-zero instead of starting. This server has no authentication and
      // its write verbs can stop or restart any PM2 process on the host, so that
      // refusal is deliberate and there is no override flag. See
      // docs/threat-model.md §1.
      //
      // CHANGING ANYTHING IN THIS FILE NEEDS MORE THAN `pm2 restart`. Measured
      // 2026-09-10: this file was edited 2026-09-09 21:17 and the process was
      // restarted 2026-09-10 05:53, yet the live process still carried the args
      // the file no longer declared. `pm2 restart` re-execs the script from disk
      // (so CODE changes do land) but re-reads its config from PM2's own dump,
      // not from here. Applying a change to this file needs `pm2 delete` +
      // `pm2 start ecosystem.config.js` — which re-captures the calling shell's
      // entire environment (the vikunja#767 mechanism) and, since #772 made the
      // shadow log actually reach disk, would now also write the NAMES of every
      // withheld variable in that shell to /home/ted/logs/pm2-mcp.log. Measured:
      // started from an agent shell that is 84 names including 12 secret-shaped
      // ones; started by PM2 at boot it is 64 names and zero secret-shaped. So
      // do that from a clean, minimal shell or at boot — never from an
      // interactive agent session.
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
