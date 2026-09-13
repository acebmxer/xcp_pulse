# Dashboard

The landing page after login. It shows the pools and hosts from the last
inventory refresh — not a live call to Xen Orchestra on every page load — so
it always says how old that information is, and if Xen Orchestra is
unreachable the last known inventory stays on screen instead of an error
where the hosts were. Press **Refresh** to ask for a current read.

Beneath the inventory, status panels summarize what the other pages own: the
latest findings severity counts, how many redaction rules are switched off,
how much the data volume is holding, and the most recent jobs. Each links to
the page that owns that information — the dashboard is a summary, not a
second copy.
