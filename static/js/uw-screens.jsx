/* Screens — scanner landing, ticker view, scanning + empty states. */
const { UW, FONT_HEAD, FONT_MONO, FONT_BODY, stateColor } = window.UW_T;

function ScreenHeader({ title, asOf, onBack }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 14, gap: 12 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 14, minWidth: 0 }}>
        {onBack && (
          <button onClick={onBack} style={{ background: "none", border: "none", color: UW.dim, fontFamily: FONT_HEAD, fontSize: 11, letterSpacing: 2, cursor: "pointer", padding: "2px 0", flexShrink: 0, whiteSpace: "nowrap" }}>
            ← SCANNER
          </button>
        )}
        <div style={{ fontFamily: FONT_HEAD, fontWeight: 700, fontSize: 15, letterSpacing: 3, color: UW.text, whiteSpace: "nowrap" }}>{title}</div>
      </div>
      <div style={{ fontFamily: FONT_MONO, fontSize: 10.5, color: UW.dim, flexShrink: 0 }}>as of {asOf}</div>
    </div>
  );
}

/* Landing: ticker + best direction + n/N, sorted n desc, PERFECT pinned top */
function ScannerLanding({ grid, onOpen }) {
  return (
    <div data-screen-label="Scanner landing" className="vNarrow">
      <ScreenHeader title="PRE-TRADE VERDICT" asOf={grid.asOf}></ScreenHeader>
      <div style={{ fontSize: 11, color: UW.dim, fontFamily: FONT_MONO, marginBottom: 12 }}>{grid.status}</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {grid.rows.map((r) => {
          const perfect = r.state === "PERFECT";
          return (
            <button key={r.ticker} onClick={() => onOpen(r.ticker)} style={{
              display: "flex", alignItems: "center", gap: 12, textAlign: "left", width: "100%",
              background: UW.card, border: `1px solid ${perfect ? UW.green + "55" : UW.cardEdge}`,
              borderRadius: 10, padding: "13px 16px", cursor: "pointer", color: UW.text,
            }}>
              <span style={{ fontFamily: FONT_HEAD, fontWeight: 700, fontSize: 16, letterSpacing: 1.5, width: 64, flexShrink: 0 }}>{r.ticker}</span>
              <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
                  <span style={{ fontFamily: FONT_HEAD, fontSize: 12, letterSpacing: 2, color: UW.dim }}>{r.direction}</span>
                  {r.tag && <span style={{ fontFamily: FONT_HEAD, fontSize: 10, letterSpacing: 1.5, color: UW.amber }}>{r.tag}</span>}
                </div>
                {(r.sub || r.premium_fmt) && (
                  <span style={{ fontFamily: FONT_MONO, fontSize: 10.5, color: UW.dim }}>
                    {r.sub || `${r.premium_fmt} · ${r.call_fmt} c / ${r.put_fmt} p`}
                  </span>
                )}
              </div>
              <span style={{ marginLeft: "auto", flexShrink: 0, fontFamily: perfect ? FONT_HEAD : FONT_MONO, fontSize: perfect ? 14 : 16, letterSpacing: perfect ? 2 : 0, color: perfect ? UW.green : UW.dim, textShadow: perfect ? `0 0 12px ${UW.green}55` : "none", fontWeight: perfect ? 700 : 400 }}>
                {perfect ? "PERFECT" : <React.Fragment>{r.green}<span style={{ color: UW.faint }}>/{r.total}</span></React.Fragment>}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* Ticker view: both directions, best first. On mobile, optional tab variant. */
function TickerView({ entry, tweaks, onBack, asOf }) {
  const best = entry.best, other = best === "calls" ? "puts" : "calls";
  const [dir, setDir] = React.useState(best);
  const tabs = tweaks.mobileDirections === "tabs";
  const seg = (d) => {
    const vm = entry[d];
    const active = dir === d;
    return (
      <button key={d} onClick={() => setDir(d)} aria-pressed={active} style={{
        flex: 1, background: active ? UW.card : "transparent",
        border: `1px solid ${active ? UW.cardEdge : "transparent"}`, borderRadius: 8,
        color: active ? UW.text : UW.dim, fontFamily: FONT_HEAD, fontSize: 12, letterSpacing: 2,
        padding: "8px 0", cursor: "pointer",
      }}>
        {vm.direction} · {vm.state === "PERFECT" ? "PERFECT" : `${vm.green}/${vm.total}`}
      </button>
    );
  };
  return (
    <div data-screen-label={`Ticker · ${entry.ticker}`}>
      <ScreenHeader title={entry.ticker} asOf={asOf} onBack={onBack}></ScreenHeader>
      {tabs && (
        <div className="dirTabs" style={{ display: "flex", gap: 6, marginBottom: 12, background: UW.inset, borderRadius: 10, padding: 4 }}>
          {seg(best)}{seg(other)}
        </div>
      )}
      <div className={"vGrid" + (tabs ? " tabsMode" : "")}>
        {[best, other].map((d) => (
          <div key={d} className={tabs && d !== dir ? "offCard" : ""}>
            <VerdictCard vm={entry[d]} anatomy={tweaks.anatomy} whyTreatment={tweaks.whyTreatment} density={tweaks.density} dimmed={d !== best}></VerdictCard>
          </div>
        ))}
      </div>
    </div>
  );
}

/* Scanning: the screen says what it is doing, not just that it is busy */
function ScanningState({ data }) {
  const bar = (w) => <div className="skelBar" style={{ width: w, height: 12, borderRadius: 4, background: UW.cardEdge }}></div>;
  return (
    <div data-screen-label="Scanning" className="vNarrow">
      <ScreenHeader title="PRE-TRADE VERDICT" asOf={data.asOf}></ScreenHeader>
      <div style={{ background: UW.card, border: `1px solid ${UW.cardEdge}`, borderRadius: 12, padding: "18px 16px", marginBottom: 14 }}>
        <div style={{ fontFamily: FONT_HEAD, fontWeight: 700, fontSize: 15, letterSpacing: 2.5, color: UW.text }}>{data.headline}</div>
        <div style={{ fontFamily: FONT_MONO, fontSize: 12, color: UW.green, marginTop: 6 }}>{data.progress}</div>
        <div style={{ fontFamily: FONT_BODY, fontSize: 12, color: UW.dim, marginTop: 4 }}>{data.detail}</div>
        <div style={{ fontFamily: FONT_BODY, fontSize: 11, color: UW.faint, marginTop: 8 }}>{data.note}</div>
      </div>
      <div className="vGrid">
        {[0, 1, 2].map((i) => (
          <div key={i} className="skelCard" style={{ background: UW.card, border: `1px solid ${UW.cardEdge}`, borderRadius: 12, padding: 16, display: "flex", flexDirection: "column", gap: 12 }} aria-hidden="true">
            {bar("38%")}{bar("62%")}
            <div style={{ display: "flex", flexDirection: "column", gap: 9, marginTop: 4 }}>
              {bar("85%")}{bar("78%")}{bar("88%")}{bar("72%")}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* Empty: nothing ready — say so in the product's own vocabulary, point at the closest */
function EmptyState({ data, onOpen }) {
  return (
    <div data-screen-label="No candidates" className="vNarrow">
      <ScreenHeader title="PRE-TRADE VERDICT" asOf={data.asOf}></ScreenHeader>
      <div style={{ background: UW.card, border: `1px solid ${UW.cardEdge}`, borderRadius: 12, padding: "26px 18px", textAlign: "center" }}>
        <div style={{ fontFamily: FONT_HEAD, fontWeight: 700, fontSize: 22, letterSpacing: 2.5, color: UW.text }}>{data.headline}</div>
        <div style={{ fontFamily: FONT_BODY, fontSize: 13, color: UW.dim, marginTop: 10, lineHeight: 1.55, maxWidth: 420, marginLeft: "auto", marginRight: "auto" }}>{data.body}</div>
        <button onClick={() => onOpen(data.closest.ticker)} style={{
          marginTop: 18, background: UW.inset, border: `1px solid ${UW.cardEdge}`, borderRadius: 8,
          color: UW.text, fontFamily: FONT_MONO, fontSize: 12, padding: "10px 16px", cursor: "pointer",
        }}>
          {data.closest.label}
        </button>
        <div style={{ fontFamily: FONT_MONO, fontSize: 10.5, color: UW.faint, marginTop: 14 }}>{data.next}</div>
      </div>
    </div>
  );
}

Object.assign(window, { ScreenHeader, ScannerLanding, TickerView, ScanningState, EmptyState });
