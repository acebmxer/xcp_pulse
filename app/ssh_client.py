"""Generic SSH plumbing shared by every host-level check.

XCP-ng has no lesser dom0 account than root to create (dom0 user management
is deprecated; see ``docs/configuration.md``), so every check connects as
root, the same account Xen Orchestra itself uses. What keeps that
root-capable key from being a blanket root shell is not this code — it is a
forced-command dispatcher script on the host side, in root's
``authorized_keys``. OpenSSH runs that script for every connection made with
this key and hands it whatever the client sent as ``$SSH_ORIGINAL_COMMAND``;
the script matches that against a fixed allowlist of check names and runs
only the matching one's fixed, read-only logic, refusing anything else. This
client sends a check name (``run_check``'s ``check`` argument) as that
command — never a shell fragment, never something the host is expected to
``eval`` — so a leaked key still cannot do anything the dispatcher script
does not already explicitly allow. The exact script and the line to add to
``authorized_keys`` are in ``docs/configuration.md``.

One key, one dispatcher script, reused by every check: adding a second check
means adding a second ``case`` branch to the host script, not a second key.
"""

from __future__ import annotations

import io

import paramiko

CONNECT_TIMEOUT = 15.0

# The dispatcher script's own connectivity probe — allowlisted on every host
# regardless of which checks are configured, so testing the connection never
# depends on any one check (e.g. NIC statistics) being set up first. Proves
# the key is accepted and the dispatcher script is enforcing the allowlist;
# nothing more.
PING_CHECK = "ping"
PING_REPLY = "xcp-pulse-diag: ok"


class SshError(RuntimeError):
    """An SSH connection or a remote command could not be completed."""


class TrustOnFirstUseHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Accept a host's key the first time, then require it stay the same.

    This application runs in a container with no meaningful
    ``~/.ssh/known_hosts`` of its own, so host key trust is tracked in the
    database instead (``ssh_connection.known_host_key``/``remember_host_key``)
    and handed in here rather than read from disk. ``known_key`` is what is
    already on record for this host, or None for a host never connected to
    before. ``on_trust`` is called with the presented key exactly once, only
    when it is being trusted for the first time — a host already on record
    never calls it again, and a host whose key no longer matches raises
    instead of calling it at all.
    """

    def __init__(self, known_key: tuple[str, bytes] | None, on_trust) -> None:
        self._known_key = known_key
        self._on_trust = on_trust
        self.trusted_new_key = False

    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key) -> None:
        presented = (key.get_name(), key.asbytes())
        if self._known_key is None:
            self._on_trust(*presented)
            self.trusted_new_key = True
            return
        if presented != self._known_key:
            raise SshError(
                f"{hostname} presented a different SSH host key than the one recorded "
                "the first time this application connected to it. This could mean the "
                "host was reinstalled — or it could mean something is intercepting the "
                "connection. Verify the host's real key by another channel before "
                "clearing the stored one and reconnecting."
            )


_KEY_CLASSES: tuple[type[paramiko.PKey], ...] = (
    paramiko.Ed25519Key,
    paramiko.RSAKey,
    paramiko.ECDSAKey,
)


def load_private_key(pem_text: str, passphrase: str | None) -> paramiko.PKey:
    """Parse a PEM-format private key of any common type.

    There is no single loader that auto-detects the key type across the
    paramiko versions this project supports — ``PKey.from_private_key`` on
    the abstract base class is not usable directly (confirmed: it calls
    ``cls(file_obj=...)``, and only a concrete subclass's ``__init__`` accepts
    that). Each concrete class is tried in turn instead, the traditional
    paramiko idiom for "any of these key types".

    A key that needs a passphrase and was not given one raises
    ``PasswordRequiredException`` from whichever class actually matches the
    file — that is a specific, reliable signal and is reported as such
    immediately rather than being swallowed by trying the remaining classes.
    Every other failure (wrong type, wrong passphrase, corrupted file) comes
    back from paramiko as a bare ``SSHException`` with no further detail, so
    those are indistinguishable from each other by design, not by omission
    here.
    """
    for key_class in _KEY_CLASSES:
        try:
            return key_class.from_private_key(io.StringIO(pem_text), password=passphrase)
        except paramiko.PasswordRequiredException as exc:
            raise SshError("this private key needs a passphrase") from exc
        except paramiko.SSHException:
            continue

    if passphrase:
        raise SshError("could not read the private key — check the key format and the passphrase")
    raise SshError(
        "could not read the private key — it may need a passphrase, or the "
        "file is not a supported key format (RSA, Ed25519, ECDSA)"
    )


def run_check(
    *,
    host: str,
    port: int,
    private_key: paramiko.PKey,
    known_host_key: tuple[str, bytes] | None,
    on_trust_new_host_key,
    check: str,
    args: str = "",
    username: str = "root",
) -> tuple[str, bool]:
    """Run one named, allowlisted check on a host over SSH and return its output.

    ``check`` (plus ``args``, space-separated) is sent as the SSH exec
    request — a correctly configured host's forced-command dispatcher script
    reads it from ``$SSH_ORIGINAL_COMMAND``, matches ``check`` against its own
    allowlist, and runs only that fixed logic; a host not set up this way, or
    a ``check`` it does not recognise, refuses the connection or the command
    rather than running something unexpected.

    ``known_host_key`` and ``on_trust_new_host_key`` implement
    trust-on-first-use against this application's own database (see
    ``TrustOnFirstUseHostKeyPolicy``) rather than a system ``known_hosts``
    file this container does not meaningfully have. ``trusted_new_key`` in
    the return value tells the caller whether this run is the one that
    recorded the host's key for the first time, worth a line in a job's own
    report rather than passing silently.

    A closed connection is always cleaned up, including on failure — this
    opens no more than one connection per call and never leaves one dangling
    on an exception.
    """
    client = paramiko.SSHClient()
    policy = TrustOnFirstUseHostKeyPolicy(known_host_key, on_trust_new_host_key)
    client.set_missing_host_key_policy(policy)

    try:
        client.connect(
            host,
            port=port,
            username=username,
            pkey=private_key,
            timeout=CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
    except paramiko.AuthenticationException as exc:
        raise SshError(
            f"{host} refused the key — check it was added to root's authorized_keys"
        ) from exc
    except (paramiko.SSHException, OSError) as exc:
        raise SshError(f"could not reach {host}:{port} — {exc}") from exc

    command = f"{check} {args}".strip()
    try:
        _, stdout, stderr = client.exec_command(command, timeout=CONNECT_TIMEOUT)
        output = stdout.read().decode("utf-8", errors="replace")
        errors = stderr.read().decode("utf-8", errors="replace").strip()
        exit_status = stdout.channel.recv_exit_status()
    except (paramiko.SSHException, OSError) as exc:
        raise SshError(f"the command to {host} failed: {exc}") from exc
    finally:
        client.close()

    if exit_status != 0 and not output.strip():
        detail = errors or f"exit status {exit_status}"
        raise SshError(f"{host} ran the forced command but it failed: {detail}")
    return output, policy.trusted_new_key


__all__ = [
    "CONNECT_TIMEOUT",
    "PING_CHECK",
    "PING_REPLY",
    "SshError",
    "TrustOnFirstUseHostKeyPolicy",
    "load_private_key",
    "run_check",
]
