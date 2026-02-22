# SECURITY.md - TraderJoes Firm Security Standard

Reference this file for all operations involving keys, deployments, or agents.

## Core Rules
- Never expose API keys in code or logs.
- Rotate all API keys on the 1st of every month.
- Never run as root.
- Use dedicated email for all services.
- Review agent permissions weekly.
- Add "never share credentials" to every agent prompt.
- Enable heartbeat monitoring (!heartbeat command).
- Block unused ports (Render does this automatically).
- Daily automated backups of GitHub repo.
- Log all security-related changes to learnings/LEARNINGS.md.

## Firm-Specific Rules
- All keys are stored only in Render Environment Variables.
- Never commit keys to GitHub.
- Use Render Background Worker for all execution.

Last updated: February 22, 2026
