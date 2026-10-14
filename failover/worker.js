/* Cloud Guardian — failover worker (dormant until the server is down).
 *
 * - scheduled (every 5 min): GETs the server /health. Miss twice in a row
 *   -> setWebhook to self and serve degraded mode. Server healthy again
 *   -> deleteWebhook so the server polling loop resumes.
 * - Degraded mode serves from KV snapshot (pushed by the server) plus
 *   LIVE HTTPS APIs (Cloudflare/Hetzner/panels need no relay, no SSH).
 *   SSH-based features answer "server down, back soon".
 */

const SNAP_KEY = "fo_snap";
const MISS_KEY = "fo_miss";
const MODE_KEY = "fo_mode"; // "server" | "worker"

const esc = (s) =>
  String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;");

async function tg(token, method, params) {
  const r = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params || {}),
    signal: AbortSignal.timeout(25000),
  });
  return r.json().catch(() => ({}));
}

const send = (token, chat, text, kb) =>
  tg(token, "sendMessage", {
    chat_id: chat, text, parse_mode: "HTML",
    ...(kb ? { reply_markup: { inline_keyboard: kb } } : {}),
  });

const B = (text, data) => ({ text, callback_data: data });
const HOME = [[B("🏠 خانه", "menu")]];

async function cf(token, path, method = "GET", body) {
  const r = await fetch(`https://api.cloudflare.com/client/v4${path}`, {
    method,
    headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(25000),
  });
  const j = await r.json().catch(() => ({}));
  if (!j.success) throw new Error(((j.errors || [])[0] || {}).message || "cf-error");
  return j.result;
}

async function serve(t, upd, env, snap) {
  const admin = Number(env.ADMIN_ID);
  const msg = upd.message, cb = upd.callback_query;
  const chat = msg ? msg.chat.id : cb.message.chat.id;
  const from = msg ? msg.from.id : cb.from.id;
  if (from !== admin) return new Response("ok");
  if (cb) await tg(t, "answerCallbackQuery", { callback_query_id: cb.id });
  const data = cb ? cb.data : null;
  const text = msg ? String(msg.text || "") : "";
  const mid = cb ? cb.message.message_id : 0;

  const show = async (body, kb) => {
    if (mid) {
      try {
        const r = await tg(t, "editMessageText", {
          chat_id: chat, message_id: mid, text: body,
          parse_mode: "HTML", reply_markup: { inline_keyboard: kb },
        });
        if (r.ok) return;
      } catch (e) {}
    }
    await send(t, chat, body, kb);
  };

  if (data === "menu" || text.startsWith("/start")) {
    return (await show(
      "🤖 حالت <b>اضطراری</b> (سرور down — ورکر)\n\n" +
      "مدیریت‌های ابری فعال‌اند؛ قابلیت‌های SSH تا برگشتن سرور صبر می‌کنند.",
      [[B("☁️ کلودفلر", "cf")], [B("🖥 هتزنر", "hz")], ...HOME]
    ), new Response("ok"));
  }
  if (data === "cf" || text.startsWith("/cf")) {
    try {
      const zones = ((await cf(snap.cf_token, "/zones?per_page=50")) || [])
        .filter((z) => z.status === "active");
      return (await show(
        `☁️ کلودفلر — ${zones.length} دامنه (حالت اضطراری)`,
        [...zones.slice(0, 30).map((z) => [B(z.name, "cfz:" + z.id)]), ...HOME]
      ), new Response("ok"));
    } catch (e) {
      return (await show("❌ " + esc(e.message), HOME), new Response("ok"));
    }
  }
  if (data && data.startsWith("cfz:")) {
    const zid = data.slice(4);
    try {
      const recs = (await cf(snap.cf_token, `/zones/${zid}/dns_records?per_page=100`)) || [];
      const lines = recs.slice(0, 40).map((r) => `• <code>${esc(r.name)}</code> ${esc(r.type)}`);
      return (await show(`📋 ${recs.length} رکورد (فقط‌خواندنی در حالت اضطراری)\n` + lines.join("\n"),
        [[B("🔙 بازگشت", "cf")], ...HOME]), new Response("ok"));
    } catch (e) {
      return (await show("❌ " + esc(e.message), HOME), new Response("ok"));
    }
  }
  if (data === "hz" || text.startsWith("/hz")) {
    try {
      const r = await fetch("https://api.hetzner.cloud/v1/servers?per_page=50", {
        headers: { Authorization: "Bearer " + (snap.hz_token || "") },
        signal: AbortSignal.timeout(25000),
      });
      const j = await r.json().catch(() => ({}));
      const list = (j && j.servers) || [];
      const lines = list.slice(0, 30).map((s) => `• ${esc(s.name)} — ${esc(s.status)}`);
      return (await show(`🖥 هتزنر — ${list.length} سرور (حالت اضطراری)\n` + (lines.join("\n") || "—"),
        HOME), new Response("ok"));
    } catch (e) {
      return (await show("❌ " + esc(e.message), HOME), new Response("ok"));
    }
  }
  return new Response("ok");
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const t = env.BOT_TOKEN;
    if (url.pathname !== "/tg" || req.method !== "POST") return new Response("ok");
    const mode = (await env.BOT_KV.get(MODE_KEY, "text")) || "server";
    if (mode !== "worker") return new Response("ok");
    const snap = (await env.BOT_KV.get(SNAP_KEY, "json")) || {};
    return serve(t, await req.json().catch(() => ({})), env, snap);
  },

  async scheduled(ev, env) {
    const t = env.BOT_TOKEN;
    const admin = Number(env.ADMIN_ID);
    let alive = false;
    try {
      const r = await fetch(env.SERVER_HEALTH_URL.replace(/\/+$/, "") + "/health",
        { signal: AbortSignal.timeout(15000) });
      alive = r.ok;
    } catch (e) {}
    let miss = Number((await env.BOT_KV.get(MISS_KEY, "text")) || 0);
    miss = alive ? 0 : miss + 1;
    await env.BOT_KV.put(MISS_KEY, String(miss));
    const mode = (await env.BOT_KV.get(MODE_KEY, "text")) || "server";
    if (!alive && miss >= 2 && mode !== "worker") {
      const hook = `${url0(env)}/tg`;
      await tg(t, "setWebhook", { url: hook, drop_pending_updates: false });
      await env.BOT_KV.put(MODE_KEY, "worker");
      await send(t, admin, "⚠️ سرور ۱۰ دقیقه جواب نداد؛ ورکر مدیریت‌های ابری را ادامه می‌دهد.");
    } else if (alive && mode === "worker") {
      await tg(t, "deleteWebhook", { drop_pending_updates: false });
      await env.BOT_KV.put(MODE_KEY, "server");
      await send(t, admin, "✅ سرور برگشت؛ کنترل پس داده شد.");
    }
  },
};

function url0(env) {
  return String(env.WORKER_URL || "").replace(/\/+$/, "");
}
