# WhatsApp message templates

WhatsApp only delivers free text within 24 hours of the resident's last message. Reminders and
date changes usually go out later than that, so they're sent as **approved templates**.
Submit these two in **Meta WhatsApp Manager → Message templates → Create template**.

The template text has no product name in it, so renaming the product never needs re-approval.

The app uses them when `MESSAGE_PROVIDER=meta`. The names and language come from these settings
(the defaults are shown):

```
META_REMINDER_TEMPLATE=bin_reminder
META_CHANGE_TEMPLATE=bin_day_changed
META_TEMPLATE_LANG=en_GB
```

Set a template name to empty to send free text instead. That's useful while a template is
waiting for approval, but free text only reaches people who messaged in the last 24 hours.

---

## 1. Reminder: `bin_reminder`

| Field | Value |
|---|---|
| Category | **Utility** |
| Name | `bin_reminder` |
| Language | **English (UK)** |
| Header | none |
| Footer | none |

**Body** (copy exactly):

```
Bin reminder: {{1}} out {{2}} ({{3}}). Tap Done when that's sorted, Snooze for a nudge later, or Wrong day if that's not your collection day.
```

**Sample values** (Meta asks for one per variable):

| Variable | Meaning | Sample |
|---|---|---|
| `{{1}}` | the bins going out | `Recycling and Food waste` |
| `{{2}}` | when | `tomorrow` |
| `{{3}}` | the collection date | `Wed 4 Nov` |

Preview: *Bin reminder: Recycling and Food waste out tomorrow (Wed 4 Nov). Tap Done when that's
sorted, Snooze for a nudge later, or Wrong day if that's not your collection day.*

**Buttons**: add **Quick reply** buttons, in this order (the order matters, the app fills them by
position):

1. `Done`
2. `Snooze`
3. `Wrong day`

Taps come back through the webhook and are handled exactly like typing DONE, SNOOZE or WRONG.

---

## 2. Date change: `bin_day_changed`

| Field | Value |
|---|---|
| Category | **Utility** |
| Name | `bin_day_changed` |
| Language | **English (UK)** |
| Header | none |
| Footer | none |
| Buttons | none |

**Body** (copy exactly):

```
Change to your collection: {{1}} now goes out on {{2}} instead of {{3}}.
```

**Sample values**:

| Variable | Meaning | Sample |
|---|---|---|
| `{{1}}` | the bin | `Recycling` |
| `{{2}}` | the new date | `Thu 5 Nov` |
| `{{3}}` | the old date | `Wed 4 Nov` |

One message is sent for each bin that moved.

---

## After approval

Nothing to change in the app if you kept the default names. Check that a test reminder arrives
with the three buttons, and that tapping **Done** marks it done (it shows in the outbox table as a
`reply`).
