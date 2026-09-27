# Settings

Where you add or replace the Xen Orchestra connection — see
[First login](first-login.md) for the walkthrough. Beyond the initial setup,
this page is also where you check a connection's current state (address,
account type, whether the certificate is verified, the result of the last
test) and where you delete a connection — which removes the stored,
encrypted token too.

This page is admin-only. **Manage users**, in the top right, opens the Users
page — see [Users and roles](users-and-roles.md).

## TLS certificate

If XCP Pulse is running with built-in HTTPS on (`XCP_PULSE_ENABLE_HTTPS`), a
**TLS certificate** section appears here — otherwise it's hidden, since
there's nothing to upload to. It shows the certificate actually in use right
now: its subject, whether it's the self-signed one generated on first run or
one you uploaded, and when it expires. A self-signed certificate is what
your browser warns about until you tell it to trust the exception.

To use your own certificate instead — one from an internal CA, or already
issued for this hostname — upload the certificate (`.pem`, `.crt` or `.cer`)
and its unencrypted private key (`.pem` or `.key`). XCP Pulse checks they're
a valid, matching, unexpired pair before saving them; nginx starts serving
the new certificate immediately, no restart needed, and this section updates
to describe it.

## Host SSH connection

A single SSH connection XCP Pulse uses to reach a host directly, for
whatever Xen Orchestra has no API route for — NIC statistics today, more may
follow. One key, reused by every such check; not a separate key per check.
Configuring it is entirely optional; leave it empty and this application
never connects to a host outside the Xen Orchestra API.

Setting it up needs a change on the host itself first — see
[Configuration](configuration.md) for the dispatcher script to install and
the exact line to add to root's `authorized_keys`, and why XCP-ng leaves root
as the only account this can use. Once that is done:

- Paste the **private** key here (RSA, Ed25519 or ECDSA). It is encrypted
  before storage and never shown again — the page only shows whether one is
  stored.
- Give it a passphrase only if the key itself has one.
- **Test connection** connects to the alphabetically first host with an
  address in the stored inventory and sends a bare connectivity probe to
  confirm the key is accepted and the host's dispatcher script is enforcing
  its allowlist — this works whether or not any specific check (NIC
  statistics or otherwise) has anything further to configure. The first time
  it reaches any given host, that host's SSH key is recorded; every later
  connection to it must present the same key, or the connection is refused
  rather than silently trusted again.
- **Delete connection** removes the stored key. It does not touch anything on
  the hosts themselves — the dispatcher script you installed stays until you
  remove it yourself.

NIC statistics itself has nothing to configure: it reads every network
interface the host reports as having a real device behind it, discovered on
the host at read time, not a list typed in here.
