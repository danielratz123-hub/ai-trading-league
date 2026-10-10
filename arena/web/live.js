// Live prices in the browser: one Server-Sent Events connection to OUR
// server (/api/stream). The browser never talks to Alpaca and never sees a
// key. Live values are display only and labeled "estimated".
//
//   const feed = new LiveFeed({ onStatus, onPrices });   feed.start();
//
// onStatus(s): s.status is live | closed | connecting | reconnecting |
//   error | disabled | offline; s.label like "Live · IEX"; s.updated "HH:MM".
// onPrices(changes, quotes): changes = {ticker: {price, prev}}.
(function () {
  "use strict";

  class LiveFeed {
    constructor({ url = "/api/stream", onStatus = () => {}, onPrices = () => {} } = {}) {
      Object.assign(this, { url, onStatus, onPrices });
      this.quotes = {};
      this.state = { status: "connecting" };
      this.backoff = 1000;          // our own retry with backoff (1 s ... 60 s)
      this.es = null;
      this.timer = null;
    }

    start() {
      if (!("EventSource" in window)) { this._status({ status: "offline" }); return; }
      this._open();
      // Reconnect at once when the tab comes back or the network returns.
      document.addEventListener("visibilitychange", () => { if (!document.hidden && !this.es) this._open(); });
      window.addEventListener("online", () => { if (!this.es) this._open(); });
    }

    _open() {
      clearTimeout(this.timer);
      const es = new EventSource(this.url, { withCredentials: true });
      this.es = es;
      es.addEventListener("snapshot", e => {
        this.backoff = 1000;
        const d = JSON.parse(e.data);
        this._prices(d.quotes || {});
        this._status(d);
      });
      es.addEventListener("prices", e => {
        const d = JSON.parse(e.data);
        this._prices(d.quotes || {});
        if (d.updated) this._status({ ...this.state, updated: d.updated });
      });
      es.addEventListener("status", e => this._status(JSON.parse(e.data)));
      es.onerror = () => {
        // The stream dropped (server restart, network, login expired).
        es.close();
        this.es = null;
        this._status({ ...this.state, status: "offline" });
        this.timer = setTimeout(() => this._open(), this.backoff + Math.random() * 500);
        this.backoff = Math.min(60000, this.backoff * 2);
      };
    }

    _prices(q) {
      const changes = {};
      for (const [t, p] of Object.entries(q)) {
        const prev = this.quotes[t];
        if (prev !== p) changes[t] = { price: p, prev };
        this.quotes[t] = p;
      }
      if (Object.keys(changes).length) this.onPrices(changes, this.quotes);
    }

    _status(s) {
      this.state = s;
      this.onStatus(s);
    }

    // "Live · IEX" while streaming; otherwise when the prices are from.
    static pillText(s, closeLabel) {
      if (s.status === "live") return s.label || "Live";
      const when = s.updated ? `prices as of ${s.updated} ET` : `prices as of ${closeLabel || "the last close"}`;
      if (s.status === "reconnecting" || s.status === "offline" || s.status === "error") return `Reconnecting · ${when}`;
      if (s.status === "disabled") return `Live prices off · ${when}`;
      return `Market closed · ${when}`;
    }
  }

  // Briefly flash an element green or red when its number changes.
  LiveFeed.flash = function (el, up) {
    if (!el || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    el.classList.remove("flash-up", "flash-down");
    void el.offsetWidth;            // restart the animation
    el.classList.add(up ? "flash-up" : "flash-down");
  };

  window.LiveFeed = LiveFeed;
})();
