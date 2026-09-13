# First login and connecting to Xen Orchestra

Log in with the admin account created from your `xcp-pulse.env` file — this
only happens once, the first time XCP Pulse starts with an empty database;
see [Users and roles](users-and-roles.md) for adding further accounts
afterward. Nothing else works until XCP Pulse has a Xen Orchestra connection,
so **Settings** is where to go first.

1. Open **Settings**.
2. Enter your Xen Orchestra address (e.g. `https://xo.example.com`).
3. Create an API token in Xen Orchestra under your user's **Authentication
   tokens**, and paste it in. It is encrypted before storage and never shown
   again — replacing it means entering a new one, not editing the old one.
4. Choose the account type: **Administrator**, or **Restricted account**. This
   is Xen Orchestra's own account-level **Permissions** setting (Settings →
   Users → Admin/User) — groups, XO 5 ACLs and RBAC roles are all separate
   from it and don't change which one you pick here.
   Downloading host logs needs the `export:logs` privilege, which none of Xen
   Orchestra's built-in roles grant, and which an XO 5 ACL cannot grant either
   — it has no such action. A restricted account will have collection refused
   until a **custom RBAC role** granting `export:logs` is created and
   assigned to it (see
   [Configuration](../configuration.md#which-xen-orchestra-account-to-use)
   for the exact steps), even though it can already see pools and hosts.
5. Leave **Verify the TLS certificate** ticked unless this Xen Orchestra uses
   a self-signed certificate on a network you trust.
6. Save, then press **Test connection**. It reports whether the account is
   admin or restricted, how many pools and hosts it can see, and whether log
   collection is available — so a permissions problem shows up here, before
   you try a real collection.

Once a connection is saved, the [Dashboard](dashboard.md) queues one
automatic inventory refresh so pools and hosts appear without an extra
click.

Beyond the initial setup, [Settings](settings.md) is also where you check a
connection's current state and delete it.
