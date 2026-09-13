# Settings

Where you add or replace the Xen Orchestra connection — see
[First login](first-login.md) for the walkthrough. Beyond the initial setup,
this page is also where you check a connection's current state (address,
account type, whether the certificate is verified, the result of the last
test) and where you delete a connection — which removes the stored,
encrypted token too.

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
