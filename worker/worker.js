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
 * Secrets (wrangler secret put ...):
 *   BOT_TOKEN       Telegram bot token
 *   CHAT_ID         the only chat served
 *   WEBHOOK_SECRET  matches setWebhook's secret_token
 *   GH_TOKEN        fine-grained PAT, Actions:write on the repo
 *   GH_REPO         owner/name
 */

const INSTANT = {
  "/help": help(),
  "/start": help(),
};

// Handled by the workflow; the Worker only acknowledges these.
const DISPATCHED = [
  "/scan", "/status", "/recent", "/pending", "/questions",
  "/answer", "/brief", "/diff", "/resume",
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
    "",
    "<i>Or just type a company name to look it up.</i>",
  ].join("\n");
}

async function send(env, text) {
  await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/sendMessage`, {
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

async function dispatch(env, command) {
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
        event_type: "telegram-command",
        client_payload: { command },
      }),
    },
  );
  return r.status === 204;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("ok", { status: 200 });
    }

    // Telegram echoes the secret configured via setWebhook. Without this
    // check the endpoint is a public URL that anyone could POST to.
    const secret = request.headers.get("x-telegram-bot-api-secret-token");
    if (!env.WEBHOOK_SECRET || secret !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

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
      ctx.waitUntil(send(env, INSTANT[cmd]));
      return new Response("ok", { status: 200 });
    }

    const needsWork =
      DISPATCHED.includes(cmd) || !text.startsWith("/");

    if (needsWork) {
      // Acknowledge immediately, then dispatch. Telegram retries a
      // webhook that does not return promptly, which would duplicate the
      // command.
      ctx.waitUntil(
        (async () => {
          const ok = await dispatch(env, text);
          if (!ok) {
            await send(
              env,
              "⚠️ Could not reach GitHub to run that.\n" +
                "<i>Scheduled scans are unaffected.</i>",
            );
          }
        })(),
      );
      return new Response("ok", { status: 200 });
    }

    ctx.waitUntil(send(env, `Unknown command: <code>${cmd}</code>\n\n${help()}`));
    return new Response("ok", { status: 200 });
  },
};
