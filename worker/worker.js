/**
 * Telegram webhook on Cloudflare Workers.
 *
 * GitHub Actions cannot host an always-on poller inside the free minute
 * budget: a 5-minute cadence alone would need ~8,600 runs/month against a
 * 2,000-minute allowance. So commands used to be answered only during a
 * scheduled scan — up to 12 hours late.
 *
 * This Worker takes the webhook instead. It is free (100k requests/day,
 * no card), always on, and replies immediately. It cannot read the
 * SQLite tracker, so anything needing real data is dispatched to Actions
 * via repository_dispatch and the workflow answers in Telegram directly.
 *
 * One Worker, two bots. The job-scout bot is served at "/" (unchanged
 * below). The LinkedIn draft bot (src/social_bot.py) is served at
 * "/social" — same account, same GH_TOKEN/GH_REPO, same deploy, just two
 * extra secrets (SOCIAL_BOT_TOKEN, SOCIAL_WEBHOOK_SECRET) and a second
 * webhook registration. They are fully independent bots: different
 * token, different webhook secret, different Actions workflow
 * (social.yml vs scout.yml), different database (data/social.db vs
 * data/jobs.db). Sharing the Worker is purely infrastructure reuse —
 * routing only, no logic or secret is shared between the two bots below.
 *
 * Secrets (wrangler secret put ...):
 *   BOT_TOKEN             job-scout bot token
 *   SOCIAL_BOT_TOKEN      LinkedIn draft bot token
 *   CHAT_ID               the only chat served (same person, both bots)
 *   WEBHOOK_SECRET        job-scout bot's setWebhook secret_token
 *   SOCIAL_WEBHOOK_SECRET LinkedIn bot's setWebhook secret_token — a
 *                         DIFFERENT value from WEBHOOK_SECRET, on
 *                         purpose. Do not reuse one for both; that was
 *                         the exact bug fixed on 2026-08-07.
 *   GH_TOKEN              fine-grained PAT, Actions:write on the repo
 *   GH_REPO               owner/name
 */

const INSTANT = {
  "/help": help(),
  "/start": help(),
};

// Handled by the workflow; the Worker only acknowledges these.
const DISPATCHED = [
  "/scan", "/status", "/recent", "/pending", "/questions",
  "/answer", "/brief", "/diff", "/resume",
  "/jd", "/applied", "/note",   // Phase 5 call-prep commands
  "/apply",                     // real-submit flow, added 2026-08-07
];

function help() {
  return [
    "<b>Commands</b>",
    "",
    "/scan — run a job scan now",
    "/status — pipeline counts + last run",
    "/recent [n] — latest matches",
    "/pending — awaiting your decision",
    "/questions — form questions needing you",
    "/answer &lt;n&gt; &lt;text&gt; — answer question n",
    "/brief &lt;company&gt; — what did I send them?",
    "/diff &lt;company&gt; — resume changes for that job",
    "/resume &lt;company&gt; — send the tailored PDF",
    "/jd &lt;company&gt; — job description as it was when you applied",
    "/applied &lt;company&gt; — mark that you actually submitted it",
    "/note &lt;company&gt; &lt;text&gt; — save a note after a call",
    "/apply &lt;company&gt; — start a real submission (you'll confirm before it sends)",
    "",
    "<i>Or just talk normally — the bot understands plain English too.</i>",
  ].join("\n");
}

async function send(env, botToken, text) {
  await fetch(`https://api.telegram.org/bot${botToken}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      chat_id: env.CHAT_ID,
      text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
    }),
  });
}

// Telegram requires this within ~a few seconds of a button tap, or the
// client shows a stuck loading spinner. The Worker answers immediately;
// the actual work still goes through dispatch() below.
async function answerCallback(env, botToken, callbackId, text) {
  await fetch(
    `https://api.telegram.org/bot${botToken}/answerCallbackQuery`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ callback_query_id: callbackId, text }),
    },
  );
}

async function dispatch(env, eventType, payload) {
  const r = await fetch(
    `https://api.github.com/repos/${env.GH_REPO}/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.GH_TOKEN}`,
        accept: "application/vnd.github+json",
        "content-type": "application/json",
        // GitHub rejects API requests without a User-Agent.
        "user-agent": "job-scout-worker",
      },
      body: JSON.stringify({
        event_type: eventType,
        client_payload: payload,
      }),
    },
  );
  return r.status === 204;
}

function socialHelp() {
  return [
    "<b>LinkedIn draft bot</b>",
    "",
    "Send any text — a topic, a bullet, a raw idea — and I'll draft a " +
      "post from it and send it back in a tap-to-copy block. Paste it " +
      "into LinkedIn yourself; this bot never posts anything on its own.",
    "",
    "/drafts — pending drafts",
    "/status — recent activity",
    "/help — this message",
  ].join("\n");
}

async function handleJobBot(request, env, ctx) {
  let update;
  try {
    update = await request.json();
  } catch {
    return new Response("bad json", { status: 400 });
  }

  const msg = update.message || update.edited_message;
  if (!msg) return new Response("ok", { status: 200 });

  // Only PJ's chat is served. The bot username is guessable, so without
  // this anyone who finds it could query his job pipeline.
  if (String(msg.chat?.id) !== String(env.CHAT_ID)) {
    return new Response("ok", { status: 200 });
  }

  const text = (msg.text || "").trim();
  if (!text) return new Response("ok", { status: 200 });

  const cmd = text.split(/\s+/)[0].toLowerCase();

  if (INSTANT[cmd]) {
    ctx.waitUntil(send(env, env.BOT_TOKEN, INSTANT[cmd]));
    return new Response("ok", { status: 200 });
  }

  const needsWork = DISPATCHED.includes(cmd) || !text.startsWith("/");

  if (needsWork) {
    // Acknowledge immediately, then dispatch. Telegram retries a
    // webhook that does not return promptly, which would duplicate the
    // command.
    ctx.waitUntil(
      (async () => {
        const ok = await dispatch(env, "telegram-command", { command: text });
        if (!ok) {
          await send(
            env,
            env.BOT_TOKEN,
            "⚠️ Could not reach GitHub to run that.\n" +
              "<i>Scheduled scans are unaffected.</i>",
          );
        }
      })(),
    );
    return new Response("ok", { status: 200 });
  }

  ctx.waitUntil(
    send(env, env.BOT_TOKEN, `Unknown command: <code>${cmd}</code>\n\n${help()}`),
  );
  return new Response("ok", { status: 200 });
}

// Fully independent from handleJobBot above — different token, different
// Actions workflow (social.yml), different database. Drafting needs a
// Gemini call, which the Worker cannot wait on reliably, so almost
// everything here dispatches to Actions rather than answering inline.
async function handleSocialBot(request, env, ctx) {
  let update;
  try {
    update = await request.json();
  } catch {
    return new Response("bad json", { status: 400 });
  }

  const cq = update.callback_query;
  if (cq) {
    if (String(cq.message?.chat?.id) !== String(env.CHAT_ID)) {
      return new Response("ok", { status: 200 });
    }
    // Answer right away so the tap doesn't spin; the real work (mark
    // posted / discard bookkeeping) happens in the dispatched workflow.
    ctx.waitUntil(answerCallback(env, env.SOCIAL_BOT_TOKEN, cq.id, ""));
    ctx.waitUntil(
      dispatch(env, "social-callback", {
        callback: {
          id: cq.id,
          data: cq.data,
          message: { message_id: cq.message?.message_id },
        },
      }),
    );
    return new Response("ok", { status: 200 });
  }

  const msg = update.message || update.edited_message;
  if (!msg) return new Response("ok", { status: 200 });

  if (String(msg.chat?.id) !== String(env.CHAT_ID)) {
    return new Response("ok", { status: 200 });
  }

  const text = (msg.text || "").trim();
  if (!text) return new Response("ok", { status: 200 });

  if (text === "/help" || text === "/start") {
    ctx.waitUntil(send(env, env.SOCIAL_BOT_TOKEN, socialHelp()));
    return new Response("ok", { status: 200 });
  }

  // Everything else — /status, /drafts, or freeform context for a new
  // draft — needs Python (Gemini, config/social_strategy.md,
  // data/social.db), so it all goes to Actions.
  ctx.waitUntil(
    (async () => {
      const ok = await dispatch(env, "social-draft", { text });
      if (!ok) {
        await send(
          env,
          env.SOCIAL_BOT_TOKEN,
          "⚠️ Could not reach GitHub to draft that. Try again shortly.",
        );
      }
    })(),
  );
  return new Response("ok", { status: 200 });
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("ok", { status: 200 });
    }

    const path = new URL(request.url).pathname;
    const isSocial = path === "/social";

    // Each bot gets its OWN webhook secret, not a shared one. This is
    // deliberate: on 2026-08-07 a shared WEBHOOK_SECRET meant rotating it
    // for one bot silently broke the other's webhook (403s with no
    // visible error until someone checked getWebhookInfo). Splitting
    // these means re-registering, rotating, or debugging one bot's
    // webhook can never again require touching the other's.
    const expected = isSocial ? env.SOCIAL_WEBHOOK_SECRET : env.WEBHOOK_SECRET;
    const secret = request.headers.get("x-telegram-bot-api-secret-token");
    if (!expected || secret !== expected) {
      return new Response("forbidden", { status: 403 });
    }

    if (isSocial) {
      return handleSocialBot(request, env, ctx);
    }
    return handleJobBot(request, env, ctx);
  },
};
