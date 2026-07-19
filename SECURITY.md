# Security Policy

## Supported versions

The latest released version (currently the `0.5.x` line) receives security fixes.
Older versions are not maintained.

## Reporting a vulnerability

Please report security issues **privately** via GitHub's
[private vulnerability reporting](https://github.com/gtonic/nfl_mcp/security/advisories/new)
— the repository's **Security** tab → *Report a vulnerability*.

Do **not** open a public issue for security problems.

We aim to acknowledge reports within a few days and will coordinate a fix and a
disclosure timeline with you.

## Notes on scope

- This server fetches data from third-party services (ESPN, Sleeper, CBS,
  FantasyCalc, The Odds API) and scrapes some public pages. Treat all fetched
  content as untrusted input.
- Input validation, URL/SSRF checks, content sanitization, and outbound rate
  limiting are built in — see the **Security Considerations** section of the
  [README](README.md).
- Optional API keys (e.g. `ODDS_API_KEY`) should be provided via environment
  variables and never committed.
