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
  5 hops) so a public URL cannot `302` its way into the private network.

**Opt-in bypass:** set `NFL_MCP_ALLOW_PRIVATE_URLS=1` (also `true`/`yes`/`on`)
only when the server runs in a trusted, isolated network and you intentionally
need to crawl internal hosts.

**Residual risk (DNS rebinding):** validation resolves DNS and then httpx
resolves again when connecting, so a host whose record flips between the two
lookups could still reach a private address. If your threat model includes
this, run the server behind an egress proxy/firewall that denies RFC 1918 and
link-local destinations.

## Network exposure and authentication

The MCP HTTP transport currently has **no built-in authentication** — anyone
who can reach the listening port can invoke every tool (including `crawl_url`).
Until token-based auth lands, do **not** expose the port to untrusted networks:
bind it to `localhost`, or place it behind a reverse proxy / API gateway that
enforces auth and network policy. Adding an optional bearer-token gate at the
transport layer is planned as a follow-up.
