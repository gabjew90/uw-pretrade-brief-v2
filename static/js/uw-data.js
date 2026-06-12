/* Data layer — live API with fixture fallback.
   Same-origin by default (drop the prototype into static/ and it talks to
   FastAPI directly). For cross-origin dev, open with ?api=http://host:port.
   The frontend computes nothing: responses are rendered verbatim and must
   already match the contract in "Present Contract Extensions.md". */
(function () {
  const API = new URLSearchParams(location.search).get("api") || "";

  async function getJSON(path) {
    const r = await fetch(API + path, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error("HTTP " + r.status + " on " + path);
    return r.json();
  }

  /* GET /api/grid → GridVM { asOf, status, rows[] } */
  async function loadGrid() {
    try {
      return { vm: await getJSON("/api/grid"), source: "live" };
    } catch (e) {
      return { vm: window.UW_GRID, source: "fixtures — live unreachable (" + e.message + ")" };
    }
  }

  /* GET /api/view/<ticker> → { ticker, best, calls: DirectionVM, puts: DirectionVM } */
  async function loadTicker(ticker) {
    try {
      return { vm: await getJSON("/api/view/" + encodeURIComponent(ticker)), source: "live" };
    } catch (e) {
      return { vm: window.UW_TICKERS[ticker] || null, source: "fixtures — live unreachable (" + e.message + ")" };
    }
  }

  window.UWData = { loadGrid, loadTicker, API };
})();
