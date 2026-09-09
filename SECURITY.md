# Security

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/kabirshah4/Roswell-Terminal/security/advisories/new)
rather than opening a public issue.

## Scope and threat model

Roswell is designed to run on `127.0.0.1` as a single-user application. It
holds API keys, a cached market database and, optionally, a password hash and
WebAuthn credentials.

Things worth knowing before you deploy it anywhere other than localhost:

- **Never commit `.env`.** It is gitignored. Every credential is read from the
  environment, and `.env` is loaded at startup from the project root.
- **`terminal.db` is not encrypted.** It sits in the project root and holds
  your watchlist, positions and cached data. It is gitignored.
- **Binding beyond loopback requires `ROSWELL_ALLOWED_HOSTS`.** The host check
  is deliberate: reaching the terminal from another device means setting the
  hostname explicitly. See *Locking the terminal* in the README.
- **This is not audited financial software.** It computes trading levels from
  cached data. Do not treat its output as advice.

## Supported versions

The `main` branch is the only supported version.
