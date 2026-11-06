// Demo mode: the real console page, with its API answered in the browser from recorded data.
// scripts/demo_site.py loads this before the page's own script. No server runs; verdicts stay
// in this tab and are gone on reload.
(() => {
  const realFetch = window.fetch.bind(window);
  const TIME_FIELDS = ["started_at", "ended_at", "first_seen_at", "updated_at", "summarised_at"];

  // Recorded alerts keep their spacing but move so the newest started a few minutes ago.
  const data = realFetch("demo-data.json")
    .then((response) => response.json())
    .then((recorded) => {
      const newest = Math.max(...recorded.alerts.map((a) => Date.parse(a.started_at)));
      const offset = Date.now() - 4 * 60 * 1000 - newest;
      for (const alert of recorded.alerts) {
        for (const field of TIME_FIELDS) {
          if (alert[field]) alert[field] = new Date(Date.parse(alert[field]) + offset).toISOString();
        }
      }
      return recorded;
    });

  const json = (body, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const newestFirst = (a, b) => Date.parse(b.started_at) - Date.parse(a.started_at);
  const inSite = (site) => (a) => !site || a.site_id === site;

  // The same order as the server's queue: most suspicious first, unsummarised between
  // suspicious and normal, then latest first.
  const queueOrder = (a, b) => (b.level ?? 0.5) - (a.level ?? 0.5) || newestFirst(a, b);

  function metrics(alerts, sites) {
    return sites.map(({ id }) => {
      const rows = alerts.filter((a) => a.site_id === id && a.severity === "alert");
      const judged = rows.filter((a) => a.verdict);
      const count = (test) => rows.filter(test).length;
      const tp = count((a) => a.level >= 1 && a.verdict === "suspicious");
      const fp = count((a) => a.level >= 1 && a.verdict === "normal");
      const fn = count((a) => a.level === 0 && a.verdict === "suspicious");
      return {
        site_id: id,
        alerts: rows.length,
        reviewed: judged.length,
        confirmed_suspicious: count((a) => a.verdict === "suspicious"),
        ai_flagged: count((a) => a.level >= 1),
        precision: tp + fp ? tp / (tp + fp) : null,
        recall: tp + fn ? tp / (tp + fn) : null,
        false_alarms_per_hour: fp / (7 * 24),
        median_seconds_to_review: null,
      };
    });
  }

  function assistantReply() {
    const message = "The assistant needs a language model, so it's off in this demo. Run VigiSift locally to try it.";
    const body = `event: text\ndata: ${JSON.stringify({ text: message })}\n\n`;
    return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
  }

  async function answer(url, method, options) {
    const recorded = await data;
    const { alerts, sites } = recorded;
    const params = url.searchParams;
    const path = url.pathname;
    const limit = Number(params.get("limit") || 50);
    const site = params.get("site_id");

    if (path === "/auth/me") return json(recorded.me);
    if (path === "/auth/login" || path === "/auth/logout") return new Response(null, { status: 204 });
    if (path === "/sites") return json(sites);
    if (path === "/devices") return json(recorded.devices);
    if (path === "/review-metrics") return json(metrics(alerts, sites));
    if (path === "/assistant/chat") return assistantReply();
    if (path === "/queue") {
      const rows = alerts.filter((a) => a.severity === "alert" && !a.verdict).filter(inSite(site));
      return json(rows.sort(queueOrder).slice(0, limit));
    }
    if (path === "/alerts") {
      const minimum = params.get("min_threat_level");
      const rows = alerts
        .filter(inSite(site))
        .filter((a) => minimum === null || (a.level !== null && a.level >= Number(minimum)));
      return json(rows.sort(newestFirst).slice(0, limit));
    }
    const alertPath = path.match(/^\/alerts\/([^/]+)\/([^/]+)\/(clip|verdict)$/);
    if (alertPath) {
      const [, siteId, reviewId, action] = alertPath.map(decodeURIComponent);
      const alert = alerts.find((a) => a.site_id === siteId && a.review_id === reviewId);
      if (!alert) return json({ detail: "alert_not_found" }, 404);
      if (action === "clip") return realFetch(`clips/${encodeURIComponent(reviewId)}.mp4`);
      if (method === "PUT") {
        const { verdict } = JSON.parse(options.body);
        Object.assign(alert, { verdict, verdict_by: recorded.me.username, verdict_at: new Date().toISOString() });
        return json({ site_id: siteId, review_id: reviewId, verdict, decided_at: alert.verdict_at });
      }
    }
    if (path.startsWith("/devices/") || path.startsWith("/assistant/")) {
      return json({ detail: "not_available_in_the_demo" }, 503);
    }
    return json({ detail: "not_found" }, 404);
  }

  window.fetch = (input, options = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const method = (options.method || "GET").toUpperCase();
    const isApi = url.origin === location.origin && !url.pathname.startsWith("/fonts/");
    return isApi ? answer(url, method, options) : realFetch(input, options);
  };

  // No server pushes events; the connection simply reports itself live.
  window.WebSocket = class {
    constructor() {
      setTimeout(() => this.onopen?.(), 0);
    }
    send() {}
    close() {}
  };
})();
