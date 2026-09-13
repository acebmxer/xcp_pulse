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

**What it holds.** Once connected, a Xen Orchestra API token that can read every
log on your pool. **On many instances that token has to be an admin one**, and
so holds full control of the pool rather than merely read access: the log
download requires a privilege those instances cannot grant to a restricted
account, leaving full host administration as the only way. Assume the token is
equivalent to pool admin credentials when deciding where to run XCP Pulse,
unless you have confirmed otherwise for your instance.

A restricted account is enough for inventory and API-based findings, and is
worth using where log collection is not needed. XOA below the Essential+ tier
has no role-based access control at all. Details are in
[the roadmap](docs/roadmap.md#which-xen-orchestra-account-to-use).

It also holds collected log bundles containing internal IP addresses,
hostnames, usernames, XAPI session tokens and audit trails. A single real
`xensource.log` measured during development contained 8,359 lines matching
password, secret or session-id patterns.

**Where it belongs.** On a trusted management network, served over HTTPS —
either its own built-in nginx (`XCP_PULSE_ENABLE_HTTPS=true`) or your own
reverse proxy. It is not built to be exposed to the internet. The compose
file publishes the port on all interfaces so it works on a remote server out
of the box; where a reverse proxy runs on the Docker host itself, bind the
mapping to `127.0.0.1` so nothing else can reach it directly.

**What protects it.** A single admin account whose Argon2id password hash is
supplied by configuration; the app refuses to start without one and has no
default password. Sessions are stored server-side so logout invalidates
immediately. Failed logins are throttled per address.

**Built-in HTTPS runs as the same non-root user as everything else.** Both
its ports are unprivileged, so nginx never needs root even briefly — no
traditional root-then-drop-privileges start. A self-signed certificate
generated on first run identifies the connection is encrypted, not that it
is who it claims to be; a certificate uploaded to replace it is validated
(a matching, unexpired, PEM-encoded pair) before nginx is told to use it,
and only a logged-in admin can upload one.

**What it does not do.** It never writes to your pool — no starting, stopping,
patching or reconfiguring. It never uploads anything anywhere; bundles are
downloaded by you and sent by you.

**Redaction is best-effort.** It removes what its rules match. Review a bundle
before sending it to a third party. A rule cannot know that an unusual string in
your environment is a secret.

**Both copies of a collection are downloadable.** Each collection keeps the raw
bundle as well as the redacted one, because the redacted copy is lossy and the
original is the only thing that can answer what was masked. The download list
marks each file **redacted — safe to send** or **raw — unmasked**; the raw one
is for your own troubleshooting, not for sending onward. Anyone who can log in
to XCP Pulse can download either.

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
