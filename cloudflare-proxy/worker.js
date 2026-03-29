/**
 * COMMANDER v2.1 — Binance Futures API Proxy
 * Cloudflare Worker: มุด IP ผ่าน Cloudflare edge network
 *
 * การทำงาน:
 *   VPS (Google Cloud, IP blocked) → Cloudflare Worker → Binance Futures API
 *
 * Free tier: 100,000 requests/day, 10ms CPU/request
 * Endpoint: https://binance-proxy.<your-subdomain>.workers.dev/
 */

// ============================================================
// CONFIG
// ============================================================
const BINANCE_BASE = "https://fapi.binance.com";

// Secret ที่ตั้งไว้ใน wrangler secret (ไม่ hardcode)
// ใช้ป้องกันคนอื่นมาใช้ Worker ของเรา
// ตั้งด้วย: wrangler secret put PROXY_SECRET
const ALLOWED_SECRET = typeof PROXY_SECRET !== "undefined" ? PROXY_SECRET : null;

// ============================================================
// MAIN HANDLER
// ============================================================
export default {
  async fetch(request, env, ctx) {
    // ดึง secret จาก env (wrangler secret)
    const proxySecret = env.PROXY_SECRET || null;

    // ── CORS preflight ──
    if (request.method === "OPTIONS") {
      return corsResponse(null, 204);
    }

    // ── Auth check: X-Proxy-Secret header ──
    if (proxySecret) {
      const clientSecret = request.headers.get("X-Proxy-Secret");
      if (clientSecret !== proxySecret) {
        return corsResponse(JSON.stringify({ error: "Unauthorized" }), 403);
      }
    }

    const url = new URL(request.url);

    // ── Health check: GET /health ──
    if (url.pathname === "/health") {
      return corsResponse(JSON.stringify({
        status: "ok",
        proxy: "COMMANDER Binance Proxy",
        edge: request.cf?.colo ?? "unknown",
        timestamp: Date.now(),
      }), 200);
    }

    // ── Proxy: ตัด /proxy prefix แล้วส่งไป Binance ──
    // ตัวอย่าง: /proxy/fapi/v2/account → https://fapi.binance.com/fapi/v2/account
    const pathMatch = url.pathname.match(/^\/proxy(\/.*)?$/);
    if (!pathMatch) {
      return corsResponse(JSON.stringify({ error: "Invalid path. Use /proxy/fapi/..." }), 400);
    }

    const binancePath = pathMatch[1] || "/";
    const targetURL = `${BINANCE_BASE}${binancePath}${url.search}`;

    // ── Forward request ──
    const forwardHeaders = new Headers();
    // copy headers ที่ Binance ต้องการ
    for (const key of ["X-MBX-APIKEY", "Content-Type"]) {
      const val = request.headers.get(key);
      if (val) forwardHeaders.set(key, val);
    }
    // User-Agent เป็น browser-like เพื่อลด fingerprint
    forwardHeaders.set("User-Agent", "Mozilla/5.0 (compatible; CFProxy/1.0)");

    let body = null;
    if (["POST", "PUT", "DELETE"].includes(request.method)) {
      body = await request.arrayBuffer();
    }

    try {
      const binanceResp = await fetch(targetURL, {
        method: request.method,
        headers: forwardHeaders,
        body: body,
      });

      const respBody = await binanceResp.arrayBuffer();
      const respHeaders = new Headers();
      // copy headers จาก Binance
      for (const [k, v] of binanceResp.headers.entries()) {
        // skip hop-by-hop
        if (["transfer-encoding", "connection", "keep-alive"].includes(k.toLowerCase())) continue;
        respHeaders.set(k, v);
      }
      // CORS
      respHeaders.set("Access-Control-Allow-Origin", "*");

      return new Response(respBody, {
        status: binanceResp.status,
        headers: respHeaders,
      });

    } catch (err) {
      return corsResponse(JSON.stringify({
        error: "Upstream fetch failed",
        detail: String(err),
      }), 502);
    }
  },
};

// ============================================================
// HELPERS
// ============================================================
function corsResponse(body, status) {
  return new Response(body, {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, X-MBX-APIKEY, X-Proxy-Secret",
    },
  });
}
