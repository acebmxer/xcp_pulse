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

Any signed-in user, of any role, can change their own password from the
**account menu** (press your username in the top bar), which does require the
current password.

## The activity log

Admin and operator accounts can open **Activity** from the top bar to see who
did what and when — logins and logouts, settings changed, jobs started or
deleted, users added or changed. It's a record, not a settings page: nothing
on it can be undone from there.
