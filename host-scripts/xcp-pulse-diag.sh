#!/bin/sh
# xcp-pulse-diag.sh — forced-command dispatcher for the XCP Pulse SSH key.
#
# This does NOT run on the XCP Pulse container. It runs on the XCP-ng host,
# installed as the `command="..."` value on the XCP Pulse key's line in
# root's authorized_keys (see docs/configuration.md for the exact line).
# OpenSSH runs this script for every connection made with that key and
# ignores whatever command the client actually asked for — this script only
# ever consults $SSH_ORIGINAL_COMMAND to look up a name in the allowlist
# below, it never executes it. That is what keeps a leaked key from being a
# root shell: whatever the client sends, only a case already written here can
# ever run, and only with arguments quoted and passed straight to a fixed
# command, never through a shell.
#
# To add a new XCP Pulse check, add a new case here — never a second key,
# never a second authorized_keys line.

set -eu

case "${SSH_ORIGINAL_COMMAND:-}" in
	ping)
		# Proves the key is accepted and this script is enforcing the
		# allowlist, independent of any specific check being configured —
		# what Settings -> Host SSH connection -> Test connection sends.
		echo "xcp-pulse-diag: ok"
		;;
	nic-stats)
		# Every interface with a real device behind it — this is what tells
		# a physical NIC (eth0, enp3s0, ...) apart from a bridge, bond, VLAN,
		# veth pair or loopback, none of which have a /sys/class/net/*/device
		# symlink, and none of which ethtool -S counters mean anything for.
		# No operator-configured interface list: whatever hardware is on the
		# host today is what gets checked, without a settings page to keep
		# in sync with it.
		for dev in /sys/class/net/*/device; do
			[ -e "$dev" ] || continue
			i=$(basename "$(dirname "$dev")")
			echo "===IFACE:$i==="
			/usr/sbin/ethtool -S "$i" 2>&1 || true
		done
		;;
	*)
		echo "xcp-pulse-diag: command not recognised" >&2
		exit 1
		;;
esac
