# Redaction

Masks addresses, session tokens and credentials in log text before it leaves
the machine. This page has two parts:

- **Preview** — paste any text and see what would be masked and how many
  values each rule caught. Nothing pasted here is stored; it exists purely to
  show what redaction would do before you trust it with a real bundle.
- **Rules** — switch individual rules on or off. A rule switched off leaves
  what it matches untouched everywhere redaction runs, not just here.

The eight rules:

| Rule | Masks | Placeholder |
| --- | --- | --- |
| Passwords and secrets | Values assigned to `password`, `secret`, `session_id`, `auth`, `token`, `API key` or `authorization` keys | `[SECRET]` |
| Session tokens | Xen Orchestra and XAPI `trackid` session identifiers | `[TOKEN]` |
| Email addresses | Anything shaped like `name@domain` | `[EMAIL]` |
| IPv4 addresses | Dotted-quad addresses (loopback and `0.0.0.0` are left legible) | `[IPv4]` |
| MAC addresses | Six colon- or hyphen-separated octets | `[MAC]` |
| IPv6 addresses | Colon-separated addresses (`::` and `::1` are left legible) | `[IPv6]` |
| UUIDs | Pool, host, VM, storage and network identifiers | `[UUID]` |
| Hostnames | Dotted names such as `xen01.internal.example` — bare single words are left alone | `[HOST]` |

A collection's report table shows a switched-off rule as **off**, not as zero
hits, so a partly-masked bundle is visible before you send it rather than
looking identical to a fully masked one.
