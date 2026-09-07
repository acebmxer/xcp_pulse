# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| Latest release | :white_check_mark: |
| Older tags | :x: |

XCP Pulse is pre-1.0 and under active development. Fixes go into the next
release rather than being backported.

## Threat model

Read this before deciding where to run XCP Pulse.

**What it holds.** From v0.2.0, a Xen Orchestra API token that can read every
log on your pool. From v0.4.0, collected log bundles containing internal IP
addresses, hostnames, usernames, XAPI session tokens and audit trails. A single
real `xensource.log` measured during development contained 8,359 lines matching
password, secret or session-id patterns.

**Where it belongs.** On a trusted management network, behind a reverse proxy.
The compose file binds to `127.0.0.1` by default for that reason. It is not
built to be exposed to the internet.

**What protects it.** A single admin account whose Argon2id password hash is
supplied by configuration; the app refuses to start without one and has no
default password. Sessions are stored server-side so logout invalidates
immediately. Failed logins are throttled per address.

**What it does not do.** It never writes to your pool — no starting, stopping,
patching or reconfiguring. It never uploads anything anywhere; bundles are
downloaded by you and sent by you.

**Redaction is best-effort.** It removes what its rules match. Review a bundle
before sending it to a third party. A rule cannot know that an unusual string in
your environment is a secret.

## Reporting a vulnerability

Report privately through GitHub, not in a public issue:

1. Open the [Security tab](https://github.com/acebmxer/xcp_pulse/security) of
   this repository.
2. Choose **Report a vulnerability**.
3. Describe the issue, how to reproduce it, and what an attacker gains.

Expect an acknowledgement within a few days. Please give a reasonable window for
a fix before disclosing publicly.

## Scope

In scope: authentication and session handling; credential storage; the redaction
engine failing to mask what it claims to; path traversal in bundle extraction;
anything letting one user reach another's data.

Out of scope: issues in Xen Orchestra or XCP-ng themselves — report those to
[Vates](https://github.com/vatesfr/xen-orchestra/security); anything requiring
an attacker to already have the admin password or host access; running XCP Pulse
exposed to the internet against this document's advice.
