# Update

Tells you whether a newer version of XCP Pulse is out, and applies it — an
alternative to running `docker compose pull && docker compose up -d` by
hand. Visible to admins and operators; unlike Settings, it isn't admin-only,
since keeping the app current is day-to-day running, not configuration.

This page does nothing until self-update is turned on, which it isn't by
default — see [Configuration](../configuration.md#xcp_pulse_enable_self_update)
for what that involves. Applying an update needs the Docker socket mounted
into the container, which is effectively host root, so it stays opt-in
rather than something every deployment gets automatically. With the feature
off, this page just explains that and points at the setting.

## Checking

**Check now** asks GitHub Container Registry whether a newer image has been
published, and shows **Update available** if so, with a link to the
project's releases. With self-update enabled, this also happens once a day
in the background, so you don't have to remember to look.

## Applying

**Apply update** only appears once an update is available. It pulls the new
image and restarts XCP Pulse to it — the page is briefly unavailable while
that happens, and refreshes itself automatically until it's back. This is
the same as running `docker compose pull && docker compose up -d` on the
host, done from inside the app instead of at a terminal.

If it fails partway — the pull itself failing, or the restart not
completing — the page shows what went wrong, including a command to run by
hand to finish the job if XCP Pulse can't. Dismiss clears that message once
you've dealt with it (or once it's confirmed successful) — a failure stays
on screen until dismissed, since it describes a state that's still true.
