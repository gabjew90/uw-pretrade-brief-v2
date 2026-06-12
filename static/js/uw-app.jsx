/* App — routing, data source (fixtures / live API), tweaks, contract readout. */
const { UW, FONT_HEAD, FONT_MONO, FONT_BODY } = window.UW_T;

/* deployment config (the ONLY sanctioned edit to the frozen frontend): live API by
   default — fixtures stay available via the Tweaks panel for demo mode */
const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "scenario": "live scan",
  "dataSource": "live api",
  "anatomy": "dot",
  "whyTreatment": "inline",
  "density": "cozy",
  "mobileDirections": "stack"
}/*EDITMODE-END*/;

const ROUTE_KEY = "uw_v3_route";

function ContractReadout() {
  const [results, setResults] = React.useState(window.__uwContract || []);
  React.useEffect(() => {
    const id = setInterval(() => setResults((window.__uwContract || []).slice()), 800);
    return () => clearInterval(id);
  }, []);
  const fails = results.filter((r) => !r.pass);
  return (
    <div style={{ fontFamily: FONT_MONO, fontSize: 10, lineHeight: 1.7, padding: "4px 0" }}>
      <div style={{ color: fails.length ? UW.red : UW.green, marginBottom: 2 }}>
        §5.4 checks: {results.length - fails.length}/{results.length} passing
      </div>
      {results.map((r) => (
        <div key={r.name} style={{ color: r.pass ? UW.dim : UW.red }}>
          {r.pass ? "✓" : "✗"} {r.name}{r.note ? ` — ${r.note}` : ""}
        </div>
      ))}
    </div>
  );
}

function SourceNote({ note }) {
  if (!note) return null;
  return (
    <div style={{ fontFamily: FONT_MONO, fontSize: 9.5, color: UW.faint, textAlign: "center", marginTop: 18 }}>
      data: {note}
    </div>
  );
}

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [route, setRoute] = React.useState(() => {
    try { return JSON.parse(localStorage.getItem(ROUTE_KEY)) || { view: "landing" }; }
    catch (e) { return { view: "landing" }; }
  });
  const go = (r) => { setRoute(r); try { localStorage.setItem(ROUTE_KEY, JSON.stringify(r)); } catch (e) {} };

  const live = t.dataSource === "live api";
  const [grid, setGrid] = React.useState({ vm: UW_GRID, source: null });
  const [tickers, setTickers] = React.useState({}); // live cache: ticker -> {vm, source}
  const [fetching, setFetching] = React.useState(null);

  // grid: refetch on data-source flip
  React.useEffect(() => {
    let on = true;
    if (!live) { setGrid({ vm: UW_GRID, source: null }); setTickers({}); return; }
    UWData.loadGrid().then((r) => { if (on) setGrid(r); });
    return () => { on = false; };
  }, [live]);

  // ticker: lazy-load on open in live mode
  React.useEffect(() => {
    let on = true;
    if (!live || route.view !== "ticker" || tickers[route.ticker]) { setFetching(null); return; }
    setFetching(route.ticker);
    UWData.loadTicker(route.ticker).then((r) => {
      if (!on) return;
      setTickers((c) => ({ ...c, [route.ticker]: r }));
      setFetching(null);
    });
    return () => { on = false; };
  }, [live, route, tickers]);

  React.useEffect(() => {
    const id = setTimeout(() => window.runUwContractChecks && window.runUwContractChecks(), 0);
    return () => clearTimeout(id);
  });

  const tweaks = { anatomy: t.anatomy, whyTreatment: t.whyTreatment, density: t.density, mobileDirections: t.mobileDirections };
  const scenario = t.scenario;
  const openTicker = (ticker) => go({ view: "ticker", ticker });
  const entryFor = (ticker) => (live ? (tickers[ticker] && tickers[ticker].vm) : UW_TICKERS[ticker]);
  const sourceNote = live ? (route.view === "ticker" && tickers[route.ticker] ? tickers[route.ticker].source : grid.source) : null;

  let screen = null;
  if (scenario === "scanning" && route.view !== "ticker") {
    screen = <ScanningState data={UW_SCANNING}></ScanningState>;
  } else if (scenario === "no candidates" && route.view !== "ticker") {
    screen = <EmptyState data={UW_EMPTY} onOpen={openTicker}></EmptyState>;
  } else if (route.view === "ticker") {
    const entry = entryFor(route.ticker);
    if (entry) {
      screen = <TickerView entry={entry} tweaks={tweaks} asOf={grid.vm.asOf} onBack={() => go({ view: "landing" })}></TickerView>;
    } else if (fetching) {
      screen = <ScanningState data={{ asOf: grid.vm.asOf, headline: "FETCHING " + route.ticker, progress: "/api/view/" + route.ticker, detail: "rendering the server's view model verbatim", note: "" }}></ScanningState>;
    } else {
      screen = <ScannerLanding grid={grid.vm} onOpen={openTicker}></ScannerLanding>;
    }
  } else {
    screen = <ScannerLanding grid={grid.vm} onOpen={openTicker}></ScannerLanding>;
  }

  return (
    <div data-app-root="true" style={{ minHeight: "100vh", background: UW.bg }}>
      <div className="vWrap">
        {screen}
        <SourceNote note={sourceNote}></SourceNote>
      </div>
      <TweaksPanel>
        <TweakSection label="Data"></TweakSection>
        <TweakRadio label="Source" value={t.dataSource} options={["fixtures", "live api"]} onChange={(v) => setTweak("dataSource", v)}></TweakRadio>
        <TweakSection label="Scenario (fixtures)"></TweakSection>
        <TweakRadio label="Scanner state" value={t.scenario} options={["live scan", "scanning", "no candidates"]} onChange={(v) => { setTweak("scenario", v); if (v !== "live scan") go({ view: "landing" }); }}></TweakRadio>
        <TweakSection label="Gate rows"></TweakSection>
        <TweakRadio label="Lamp anatomy" value={t.anatomy} options={["dot", "block", "edge"]} onChange={(v) => setTweak("anatomy", v)}></TweakRadio>
        <TweakRadio label="Why? layout" value={t.whyTreatment} options={["inline", "sheet"]} onChange={(v) => setTweak("whyTreatment", v)}></TweakRadio>
        <TweakSection label="Layout"></TweakSection>
        <TweakRadio label="Density" value={t.density} options={["cozy", "compact"]} onChange={(v) => setTweak("density", v)}></TweakRadio>
        <TweakRadio label="Mobile directions" value={t.mobileDirections} options={["stack", "tabs"]} onChange={(v) => setTweak("mobileDirections", v)}></TweakRadio>
        <TweakSection label="Directive §5.4"></TweakSection>
        <ContractReadout></ContractReadout>
      </TweaksPanel>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App></App>);
