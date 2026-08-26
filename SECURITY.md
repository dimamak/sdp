# Security Policy

Social Daily Poster handles credentials (LinkedIn/X tokens, Telegram bot token, API
keys) and reads coding-agent session transcripts, which can contain source
code and secrets from your own projects. Please report vulnerabilities
privately rather than in a public issue.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository: open the
**Security** tab → **Report a vulnerability**. This goes directly to the
maintainer and is not visible publicly until resolved.

Please include:

- What component is affected (harvester, drafting pipeline, Telegram bot,
  LinkedIn/X/Reddit delivery, setup wizard).
- Steps to reproduce, or the code path that shows the issue.
- What you'd expect to happen instead.

## Scope

In scope: credential handling, injection risks in harvested content reaching
an LLM prompt or a posting API, anything that could cause an unintended
publish or leak transcript contents. Platform ban/ToS risk from WhatsApp/X/
Reddit automation is a known, documented tradeoff (see the README's
[Limitations](README.md#limitations--non-goals)) rather than a vulnerability,
but if you've found a way it's worse than documented, that's still worth
reporting.

Out of scope: vulnerabilities in third-party services this project talks to
(LinkedIn, X, Reddit, Telegram, WAHA, Gemini) — report those to the
respective vendor.
