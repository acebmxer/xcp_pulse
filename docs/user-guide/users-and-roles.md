# Users and roles

XCP Pulse supports any number of accounts, each with one role:

| Role | Can do |
| --- | --- |
| **Admin** | Everything, including managing users, the Xen Orchestra connection, and redaction rules. |
| **Operator** | Run and delete collections, redactions, extractions and support packages; download anything stored; view the Activity page. Cannot change settings, redaction rules, or users. |
| **Viewer** | Read-only: dashboard, findings, jobs, activity log — and can still download anything already stored. Cannot start or delete anything, or change any setting. |

## Adding a user

1. Open **Settings** (admin-only).
2. Press **Manage users**.
3. Under **Add a user**, enter a username, a password (8 characters minimum),
   and a role.
4. Press **Add user**.

## Changing a role, or disabling an account

On the Users page, each row has its own role dropdown — changing it saves
immediately. **Disable** stops that account from logging in and ends any
session it already has open, without deleting the account or its history in
the activity log. **Enable** reverses it.

XCP Pulse will not let the last admin account be disabled or changed to
another role — there always has to be at least one way to manage the app.

## Resetting someone's password

Also on the Users page: type a new password next to their name and press
**Reset password**. This does not need their current password — it's for
when they've lost it.

## Changing your own password

Any signed-in user, of any role, can change their own password from
**Change password / 2FA**, in the **☰ Menu** dropdown in the top bar. This
does require the current password.

## Two-factor authentication

Any signed-in user can turn on TOTP two-factor for their own account from the
same **Change password / 2FA** screen. Once it's on, signing in needs a code
from an authenticator app (Google Authenticator, Authy, 1Password, etc.) as
well as your password — this is per-account, not something an admin turns on
for everyone.

1. Open **☰ Menu**, then **Change password / 2FA**.
2. Under Two-factor authentication, press **Set up two-factor authentication**.
3. Scan the QR code with your authenticator app, or type the code shown
   beneath it in by hand if you can't scan.
4. Enter the 6-digit code the app is now showing, to confirm it's working.
5. Save the ten backup codes shown next, somewhere safe — each works once, in
   place of an authenticator code, if you lose the device. They are shown
   only this once.

To turn it off, open **Change password / 2FA** and enter your current
password. If someone loses both their device and their backup codes, an admin
can turn off their two-factor authentication for them from the Users page —
no code or password needed, the same recovery role a password reset plays.

## The activity log

Admin and operator accounts can open **Activity** (`/activity`) to see who did
what and when — logins and logouts, settings changed, jobs started or deleted,
users added or changed. It's a record, not a settings page: nothing on it can
be undone from there. It's not linked from the top bar; go to `/activity`
directly.
