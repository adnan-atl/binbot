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
Bin reminder: {{1}} out {{2}} ({{3}}). Tap Done when that's sorted, Snooze for a nudge later, or Settings to change your bins or time.
```

**Sample values** (Meta asks for one per variable):

| Variable | Meaning | Sample |
|---|---|---|
| `{{1}}` | the bins going out | `Recycling and Food waste` |
| `{{2}}` | when | `tomorrow` |
| `{{3}}` | the collection date | `Wed 4 Nov` |

Preview: *Bin reminder: Recycling and Food waste out tomorrow (Wed 4 Nov). Tap Done when that's
sorted, Snooze for a nudge later, or Settings to change your bins or time.*

**Buttons**: add **Quick reply** buttons, in this order (the order matters, the app fills them by
position):

1. `Done`
2. `Snooze`
3. `Settings`

**Message validity period**: custom, **3 hours** (Meta's default is 10 minutes, too short if a
phone is off for a bit).

Taps come back through the webhook and are handled exactly like typing DONE or SNOOZE.
**Settings** opens the settings menu (reminder time, bins, wrong collection day, pause), all by
tapping. Typing WRONG still works too.

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
| Validity period | custom, **3 hours** |

**Body** (copy exactly):

```
Change to your collection: {{1}} now goes out on {{2}} instead of {{3}}. Your reminders have moved to match.
```

(Meta rejects a body that ends on a variable, hence the second sentence.)

**Sample values**:

| Variable | Meaning | Sample |
|---|---|---|
| `{{1}}` | the bin | `Recycling` |
| `{{2}}` | the new date | `Thu 5 Nov` |
| `{{3}}` | the old date | `Wed 4 Nov` |

One message is sent for each bin that moved.

---

## 3. Reminder with referral news (optional): `bin_reminder_news`

The same reminder with one extra line when neighbours have joined with the resident's invite link.
News is batched and only ever rides on a reminder, never a message of its own. Until this one is
approved (leave `META_REMINDER_NEWS_TEMPLATE` empty), the news goes in the reply to Done / Snooze.

| Field | Value |
|---|---|
| Category | **Utility** |
| Name | `bin_reminder_news` |
| Language | **English (UK)** |
| Header | none |
| Footer | none |

**Body** (copy exactly):

```
Bin reminder: {{1}} out {{2}} ({{3}}).

{{4}}

Tap Done when that's sorted, Snooze for a nudge later, or Settings to change your bins or time.
```

**Sample values**:

| Variable | Meaning | Sample |
|---|---|---|
| `{{1}}` | the bins going out | `Recycling and Food waste` |
| `{{2}}` | when | `tomorrow` |
| `{{3}}` | the collection date | `Wed 4 Nov` |
| `{{4}}` | the referral news | `🎉 2 neighbours joined with your link. You're now a Kerbside regular. 1 more to Street captain.` |

**Buttons**: the same three Quick reply buttons, same order: `Done`, `Snooze`, `Settings`.
**Message validity period**: 3 hours.

If Meta files it under **Marketing** instead of Utility, don't use it (marketing messages cost
more and need marketing opt-in): leave `META_REMINDER_NEWS_TEMPLATE` empty and the news keeps
riding on the Done / Snooze reply. Once approved, set `META_REMINDER_NEWS_TEMPLATE=bin_reminder_news`.

---

## After approval

Nothing to change in the app if you kept the default names. Check that a test reminder arrives
with the three buttons, and that tapping **Done** marks it done (it shows in the outbox table as a
`reply`).
