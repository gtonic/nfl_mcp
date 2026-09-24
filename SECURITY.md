# Security Policy

## Supported versions

The latest released version (currently the `0.6.x` line) receives security fixes.
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

## SSRF protection for `crawl_url`

`crawl_url` is the only tool that fetches an **arbitrary, caller-supplied**
URL. Before any request is made it is validated by `is_safe_public_url()`,
which:

- requires an `http://` / `https://` scheme;
- resolves the host via DNS and **blocks any address** that is loopback,
  private (RFC 1918), link-local — including the `169.254.169.254` cloud
  metadata endpoint — multicast, reserved, unspecified or otherwise
  non-global. IP literals in decimal/octal/IPv6/IPv4-mapped form are
  normalized and checked, so `http://2130706433/` or `http://[::1]/` are
  rejected too;
- re-validates **every redirect hop** (redirects are followed manually, up to
  5 hops) so a public URL cannot `302` its way into the private network;
- only targets ports 80/443, runs under a 20 s total time budget, requests
  `Accept-Encoding: identity` and inflates any compressed body itself with a
  bounded decoder, so a small gzip bomb cannot expand past the byte cap
  (`NFL_MCP_CRAWL_MAX_BYTES`, default 2 MB).

**Opt-in bypass:** set `NFL_MCP_ALLOW_PRIVATE_URLS=1` (also `true`/`yes`/`on`;
this also lifts the port restriction) only when the server runs in a trusted, isolated network and you intentionally
need to crawl internal hosts.

**Residual risk (DNS rebinding):** validation resolves DNS and then httpx
resolves again when connecting, so a host whose record flips between the two
lookups could still reach a private address. If your threat model includes
this, run the server behind an egress proxy/firewall that denies RFC 1918 and
link-local destinations.

## Network exposure and authentication

The server binds `127.0.0.1` by default (the Docker image binds `0.0.0.0`
inside the container — publish it as `-p 127.0.0.1:9000:9000`). Every request's
`Host` must be localhost/127.0.0.1/[::1] or listed in `NFL_MCP_ALLOWED_HOSTS`
(421 otherwise), and a present `Origin` must be loopback, same-origin or listed
in `NFL_MCP_ALLOWED_ORIGINS` (403 otherwise) — this blocks DNS-rebinding
attacks from a web page against a local server.

Authentication is opt-in: set `NFL_MCP_AUTH_TOKEN` and `/mcp` requires
`Authorization: Bearer <token>` (constant-time comparison); `/metrics` too, and
`/health` returns only status/version without it. Without a token anyone who
can reach the port can invoke every tool (including `crawl_url`), so set one
before exposing the port beyond localhost, and terminate TLS in a reverse proxy
when crossing an untrusted network.
