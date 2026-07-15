# Security Policy

## Supported versions

MatterRegistry ships as a single Docker image and is developed as a
rolling release. Only the most recent published version receives security
fixes - please upgrade to the latest tag before reporting an issue.

| Version                | Supported |
| ---------------------- | --------- |
| Latest release (0.3.x) | Yes       |
| Any older release      | No        |

## Reporting a vulnerability

Please report security problems **privately** - do not open a public issue,
pull request, or discussion for a suspected vulnerability.

- **Preferred:** use GitHub's private vulnerability reporting:
  [Report a vulnerability](https://github.com/Jaano/matterregistry/security/advisories/new).
  This opens a private advisory visible only to you and the maintainer, so
  the issue can be discussed and fixed before any public disclosure.

Please include:

- the MatterRegistry version,
- the deployment mode (Home Assistant App or standalone Docker),
- steps to reproduce and the impact you observed,
- any relevant logs, with tokens, pairing PINs, and other secrets redacted.

This is a hobby project maintained in spare time. Reports are acknowledged
and triaged as soon as reasonably possible; please allow time for a fix
before disclosing publicly. Coordinated disclosure is appreciated.

## Scope - intentional design decisions

The following behaviors are deliberate, documented design choices, not
vulnerabilities. Reports about them will be closed as by-design:

- **No application-level authentication.** MatterRegistry has no login,
  user accounts, or per-user tokens. Access control is delegated to the
  network layer - Home Assistant Ingress on the HA path, or your own
  firewall / reverse proxy for a standalone deployment. Exposing the HTTP
  port directly to an untrusted network is not a supported configuration.
- **Secrets are stored in plaintext at rest.** By design, the SQLite
  database and the JSON exports contain pairing PINs, manual setup codes,
  QR payloads, integration tokens, and Thread network keys in cleartext.
  This tool is the durable home for exactly the metadata that the Matter
  and HomeKit commissioning specs discard - protecting the data volume and
  any backups is the operator's responsibility.

Reports about ways to **bypass the network-layer boundary**, leak data
across it, or otherwise subvert the intended trust model - as well as the
usual web vulnerabilities (injection, SSRF, path traversal, and the like) -
are in scope and welcome.
