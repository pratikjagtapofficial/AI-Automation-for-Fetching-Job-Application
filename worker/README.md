# Telegram webhook — Cloudflare Worker

Makes bot commands work 24/7 without anything running on PJ's machine.

**Why a Worker.** GitHub Actions cannot host an always-on poller inside
the free minute budget — a 5-minute cadence alone needs ~8,600 runs/month
against a 2,000-minute allowance. Without this, commands are only
answered during a scheduled scan, so a message sent at 3pm waits until
2am. Cloudflare Workers are free (100k requests/day), need no card, and
are always on.

**What it does.** Verifies the request, serves only PJ's chat, replies to
`/help` instantly, and dispatches everything else to GitHub Actions,
which does the work and answers in Telegram. Real answers arrive in under
a minute instead of up to twelve hours.

---

## Setup

### 1. Cloudflare account
Sign up at <https://dash.cloudflare.com/sign-up>. Free plan, no card.

### 2. Install wrangler and log in
```bash
npm install -g wrangler
wrangler login
```

### 3. GitHub token
The Worker needs to trigger workflows. Create a **fine-grained PAT** at
<https://github.com/settings/personal-access-tokens/new>:

- Repository access: **only** this repository
- Permissions: **Actions → Read and write**, **Contents → Read and write**
- Expiry: whatever you're comfortable re-issuing

Add it to the repo as a secret named `GH_DISPATCH_TOKEN` too, so
`/scan` works from the workflow side.

### 4. Deploy
```bash
cd worker
wrangler deploy
```
Note the printed URL — `https://job-scout-bot.<subdomain>.workers.dev`.

### 5. Secrets
Run each and paste the value when prompted:
```bash
wrangler secret put BOT_TOKEN
wrangler secret put CHAT_ID
wrangler secret put WEBHOOK_SECRET
wrangler secret put GH_TOKEN
wrangler secret put GH_REPO
```

- `BOT_TOKEN` — the Telegram bot token
- `CHAT_ID` — `1858655236`
- `WEBHOOK_SECRET` — any random string you invent; it must match step 6
- `GH_TOKEN` — the PAT from step 3
- `GH_REPO` — `pratikjagtapofficial/AI-Automation-for-Fetching-Jobs-Job-Application`

### 6. Point Telegram at the Worker
```bash
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -d "url=https://job-scout-bot.<subdomain>.workers.dev" \
  -d "secret_token=<WEBHOOK_SECRET>"
```

`secret_token` is what makes the endpoint safe: Telegram echoes it on
every request, and the Worker rejects anything without it. The URL is
public, so without this check anyone could POST to it.

### 7. Verify
Send `/help` in Telegram — instant. Then `/status` — acknowledged
instantly, real answer within a minute.

---

## Notes

**Cost.** Each dispatched command starts an Actions run, and Actions
bills a minimum of one minute. Twenty commands a day is ~20 minutes
against a 66-minute daily budget, alongside the scans. Heavy command use
is the one thing that could strain the free tier.

**Switching back.** Polling still works for local use:
```bash
python -m src.bot
```
Remove the webhook first — Telegram will not deliver to both:
```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/deleteWebhook"
```

**Rotating the bot token.** It must be updated in three places: the repo
secret `TELEGRAM_BOT_TOKEN`, the Worker secret `BOT_TOKEN`, and the
`setWebhook` call in step 6.
