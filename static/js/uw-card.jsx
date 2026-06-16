/* Verdict card + gate lamp rows. Anatomy variants (dot / block / edge) and the
   why-expansion treatment (inline / sheet) are the design-owned surface; the
   vocabulary, gate labels, chart count and data are locked by the directive. */
const { UW, FONT_HEAD, FONT_MONO, FONT_BODY, stateColor } = window.UW_T;

const SR_STATE = { green: "condition met", red: "condition failed", dark: "no data" };

function Lamp({ state, anatomy }) {
  const col = stateColor(state);
  if (anatomy === "block") {
    return <span aria-hidden="true" style={{
      width: 20, height: 11, borderRadius: 3, flexShrink: 0,
      background: state === "dark" ? "transparent" : col,
      border: state === "dark" ? `1px solid ${UW.gray}` : "1px solid transparent",
      boxShadow: state === "green" ? `0 0 8px ${UW.green}88` : "none",
      opacity: state === "dark" ? 0.7 : 1,
    }}></span>;
  }
  return <span aria-hidden="true" style={{
    width: 10, height: 10, borderRadius: "50%", flexShrink: 0, background: col,
    boxShadow: state === "green" ? `0 0 7px ${UW.green}99` : "none",
    opacity: state === "dark" ? 0.45 : 1,
  }}></span>;
}

/* why-panel = the metric TABLE (data grid, not prose): one row per sub-criterion with
   its own pass/fail mark, value text, plus the one-line caption and a provenance footer.
   The visual itself lives on the gate row now (data/visual-centric, operator 2026-06-16). */
function WhyInset({ gate }) {
  const w = gate.why || {};
  const mark = (s) => (s === "green" ? "✓" : s === "red" ? "✗" : "·");
  return (
    <div data-why-inset="true" data-for-gate={gate.name} style={{ background: UW.inset, borderRadius: 6, padding: "8px 12px", marginTop: 4 }}>
      {w.caption && <div style={{ fontSize: 10.5, color: UW.dim, fontFamily: FONT_MONO, marginBottom: 6, lineHeight: 1.5 }}>{w.caption}</div>}
      {(w.rows || []).map((r, i) => (
        <div key={i} style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "2px 0", fontSize: 10.5, fontFamily: FONT_MONO }}>
          <span style={{ color: stateColor(r.state || "dark"), width: 10, flexShrink: 0 }}>{mark(r.state)}</span>
          <span style={{ color: UW.text, width: 110, flexShrink: 0, textTransform: "capitalize" }}>{r.label}</span>
          <span style={{ color: UW.dim }}>{r.text}</span>
        </div>
      ))}
      {(w.missing || []).map((m, i) => (
        <div key={"m" + i} style={{ fontSize: 10.5, color: UW.dim, fontFamily: FONT_MONO, padding: "2px 0" }}>· {m}</div>
      ))}
      {w.prov && <div data-prov="true" style={{ fontSize: 9, color: UW.dim, opacity: 0.8, fontFamily: FONT_MONO, marginTop: 6 }}>{w.prov}</div>}
    </div>
  );
}

function MicroVisual({ gate }) {
  const w = gate.why || {};
  if (gate.name === "no_squeeze") return <Checks items={w.items || []}></Checks>;
  if (gate.state === "dark") return <DarkNote lines={w.missing || ["no data this cycle"]}></DarkNote>;
  const d = w.data || {};
  switch (w.kind) {
    case "tug": return <TugOfWar {...d} state={gate.state}></TugOfWar>;
    case "ladder": return <Ladder {...d} state={gate.state}></Ladder>;
    case "cheap_vol": return <CheapVol {...d} state={gate.state}></CheapVol>;
    case "runway": return <Runway {...d} state={gate.state}></Runway>;
    case "dot_strip": return <DotStrip {...d} state={gate.state}></DotStrip>;
    default: return null;
  }
}

function GateRow({ gate, anatomy, density, children }) {
  const edge = anatomy === "edge";
  return (
    <div data-gate-row="true" data-gate-name={gate.name} style={{
      padding: density === "compact" ? "6px 0" : "9px 0",
      borderBottom: `1px solid ${UW.cardEdge}`,
      borderLeft: edge ? `3px solid ${stateColor(gate.state)}` : "none",
      paddingLeft: edge ? 10 : 0,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 11 }}>
        {!edge && <Lamp state={gate.state} anatomy={anatomy}></Lamp>}
        <span style={{ fontSize: 13, color: gate.state === "dark" ? UW.dim : UW.text, fontFamily: FONT_BODY, textTransform: "capitalize" }}>{gate.short || gate.label}</span>
        <span className="visually-hidden">{SR_STATE[gate.state]} {gate.label}</span>
        <span style={{ marginLeft: "auto", flexShrink: 0, fontSize: 11, fontFamily: FONT_MONO, letterSpacing: gate.state === "dark" ? 1 : 0, color: gate.state === "dark" ? UW.dim : stateColor(gate.state) }}>
          {gate.state === "dark" ? "NO DATA" : gate.metric}
        </span>
      </div>
      {children && <div style={{ marginTop: 6, marginLeft: edge ? 0 : 21 }}>{children}</div>}
    </div>
  );
}

function NumbersGrid({ numbers, density }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: density === "compact" ? "6px 12px" : "8px 14px", padding: "12px 0 4px" }}>
      {numbers.map(([k, v]) => (
        <div key={k} data-number-row="true">
          <div style={{ fontSize: 9.5, color: UW.dim, fontFamily: FONT_BODY, textTransform: "uppercase", letterSpacing: 0.8 }}>{k}</div>
          <div style={{ fontSize: 13.5, color: UW.text, fontFamily: FONT_MONO }}>{v}</div>
        </div>
      ))}
    </div>
  );
}

function VerdictCard({ vm, anatomy = "dot", whyTreatment = "inline", density = "cozy", forceOpen, dimmed }) {
  const [openS, setOpen] = React.useState(false);
  const open = forceOpen !== undefined ? forceOpen : openS;
  const perfect = vm.state === "PERFECT";
  const openProps = open ? { "data-why-open": "true" } : {};
  return (
    <div className="vCard" data-verdict-card="true" {...openProps} style={{
      background: UW.card,
      border: `1px solid ${perfect ? UW.green + "55" : UW.cardEdge}`,
      borderRadius: 12,
      padding: density === "compact" ? "12px 14px 4px" : "16px 16px 8px",
      opacity: dimmed ? 0.82 : 1,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <div>
          <div style={{ fontFamily: FONT_HEAD, fontSize: 13, letterSpacing: 2, color: UW.dim }}>
            {vm.ticker} · {vm.direction}{vm.tag ? <span style={{ color: UW.amber }}>{"  " + vm.tag}</span> : null}
          </div>
          <div data-verdict-headline="true" className={perfect ? "perfectGlow" : ""} style={{
            fontFamily: FONT_HEAD, fontWeight: 700, fontSize: 30, letterSpacing: 3, marginTop: 2,
            whiteSpace: "nowrap", lineHeight: 1.15,
            color: perfect ? UW.green : UW.text,
            textShadow: perfect ? `0 0 18px ${UW.green}55` : "none",
          }}>{vm.state}</div>
        </div>
        {!perfect && (
          <div style={{ fontFamily: FONT_MONO, fontSize: 22, color: UW.dim }}>
            {vm.green}<span style={{ color: UW.faint }}>/{vm.total}</span>
          </div>
        )}
      </div>
      {!perfect && vm.waiting && (
        <div style={{ fontSize: 12.5, color: UW.dim, fontFamily: FONT_BODY, marginTop: 2 }}>{vm.waiting}</div>
      )}

      <div style={{ marginTop: 10 }}>
        {vm.gates.map((g) => (
          <GateRow key={g.name} gate={g} anatomy={anatomy} density={density}>
            <React.Fragment>
              {/* a compact visual on every row (data/visual-centric board): the wide
                  flow strip for smart_flow, a small micro-visual for the rest */}
              {g.flow ? <FlowSession {...g.flow} state={g.state}></FlowSession>
                : (g.state !== "dark" && (
                    <div style={{ maxWidth: 190 }}><MicroVisual gate={g}></MicroVisual></div>
                  ))}
              {/* expand: the metric table + caption + sources */}
              {open && whyTreatment === "inline" && <WhyInset gate={g}></WhyInset>}
            </React.Fragment>
          </GateRow>
        ))}
      </div>

      {open && whyTreatment === "sheet" && (
        <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 10 }}>
          {vm.gates.map((g) => (
            <div key={g.name}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
                <Lamp state={g.state} anatomy="dot"></Lamp>
                <span style={{ fontSize: 10, letterSpacing: 1.5, textTransform: "uppercase", color: UW.dim, fontFamily: FONT_HEAD }}>{g.short}</span>
              </div>
              <WhyInset gate={g}></WhyInset>
            </div>
          ))}
        </div>
      )}

      {vm.numbers && <NumbersGrid numbers={vm.numbers} density={density}></NumbersGrid>}

      <button aria-expanded={open} onClick={() => setOpen(!open)} style={{
        width: "100%", background: "none", border: "none", color: UW.dim, fontFamily: FONT_HEAD,
        fontSize: 11, letterSpacing: 2, padding: "10px 0 8px", cursor: "pointer",
      }}>
        {open ? "— HIDE WHY —" : "— WHY? —"}
      </button>
    </div>
  );
}

Object.assign(window, { Lamp, GateRow, WhyInset, MicroVisual, NumbersGrid, VerdictCard });
