# Security Policy

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

To report a vulnerability, use one of these channels:

- **GitHub private disclosure:** Use the [Security tab](https://github.com/TadMSTR/pm2-mcp/security/advisories/new) to submit a private advisory.
- **Email:** Send a description to `security.i9v75@8alias.com` with the subject line `[pm2-mcp] Security Report`.

Include as much detail as possible: the affected component, steps to reproduce, and potential impact.

## Scope

**In scope:**

- Process name injection enabling unintended PM2 process operations (start/stop/delete)
- Information disclosure through process list responses exposing environment variables or credential paths
- Privilege escalation via unintended lifecycle operations on privileged PM2 processes
- Dependency vulnerabilities with a plausible exploitation path in pm2-mcp's usage

**Out of scope:**

- Vulnerabilities in the host system, underlying services, or MCP transport layer
- Issues that require attacker control of configuration environment variables
  (operator-controlled trust boundaries, not input attack surfaces)
- Theoretical weaknesses without a realistic attack path against the MCP tool surface

## Response Expectations

| Stage | Timeline |
|-------|----------|
| Acknowledgement | Within 3 business days |
| Initial assessment | Within 7 business days |
| Fix or remediation plan | Within 30 days for critical/high; 60 days for medium/low |

This is a personal project maintained by one developer. Response times are best-effort.
If you haven't heard back within 3 business days, a follow-up email is welcome.

## Disclosure

Coordinated disclosure is preferred. Please allow time for a fix to be released before
public disclosure. The CHANGELOG documents remediated findings at an appropriate level
of detail after each release.
