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
| `{{4}}` | the referral news | `🎉 2 neighbours joined with your link. 8 more and you're officially a Binfluencer.` |

**Buttons**: the same three Quick reply buttons, same order: `Done`, `Snooze`, `Settings`.
**Message validity period**: 3 hours.

If Meta files it under **Marketing** instead of Utility, don't use it (marketing messages cost
more and need marketing opt-in): leave `META_REMINDER_NEWS_TEMPLATE` empty and the news keeps
riding on the Done / Snooze reply. Once approved, set `META_REMINDER_NEWS_TEMPLATE=bin_reminder_news`.

---

## 4. Share with neighbours: `bin_share`

Sent once, the day after someone's first reminded collection (11am), unless they already got the
link by tapping **Done** on a reminder (that one goes free, inside the chat window). Only to
people who didn't opt out of updates at sign-up. Until it's approved, leave
`META_SHARE_TEMPLATE` empty: the Done path still works, and everyone waiting gets the template
on the first morning after you set it.

| Field | Value |
|---|---|
| Category | **Marketing** (it promotes the service, so Meta will file it there anyway) |
| Name | `bin_share` |
| Language | **English (UK)** |
| Header | none |
| Footer | none |
| Buttons | none |

**Body** (copy exactly):

```
Hope that made bin day easier. If you want to share {{1}} with neighbours or friends, send them this link: {{2}} 🔗
```

(Meta won't accept a template that ends with a variable, hence the emoji after the link.)

**Sample values**:

| Variable | Meaning | Sample |
|---|---|---|
| `{{1}}` | the product name | `Binjamin` |
| `{{2}}` | their invite link | `https://binjamin.uk/?ref=ab12cd34` |

Once approved, set `META_SHARE_TEMPLATE=bin_share` in the server's `.env` and restart.

---

## 5. Sign-up nudge: `bin_signup_nudge`

For website sign-ups that reached the "Say hi to Binjamin" step but never sent the message.
Twelve minutes later they get this once, with a **Start reminders** button. Tapping it counts as
their hi: reminders go on and the welcome follows (free, because they've just tapped). Only sent
to people who ticked the terms; never sent again.

There's no separate "setup" or "welcome" template: the welcome is always free text, because it
only ever goes out straight after the person messages or taps.

| Field | Value |
|---|---|
| Category | **Utility** (it finishes something they started) |
| Name | `bin_signup_nudge` |
| Language | **English (UK)** |
| Header | none |
| Footer | none |

**Body** (copy exactly, no variables):

```
Your bin reminders are nearly ready. Tap Start reminders to switch them on, or ignore this if it wasn't you.
```

**Buttons**: one **Quick reply** button: `Start reminders`.

Once approved, set `META_SIGNUP_NUDGE_TEMPLATE=bin_signup_nudge` in the server's `.env` and
restart. Until then nobody is nudged (free text wouldn't reach a number that hasn't messaged us).

---

## After approval

Nothing to change in the app if you kept the default names. Check that a test reminder arrives
with the three buttons, and that tapping **Done** marks it done (it shows in the outbox table as a
`reply`).
