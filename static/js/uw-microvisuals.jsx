/* Micro-visuals — one shared grammar: a marker vs a green-shaded pass-zone,
   VALUE-ANCHORED (real numbers at endpoints, threshold, marker). No gridlines,
   no axis ticks, no legends. Geometry is the only client math; every number,
   label and caption string arrives in the view model. */
const { UW, FONT_HEAD, FONT_MONO, FONT_BODY, stateColor } = window.UW_T;

/* tug-of-war: dominant-side share vs the 70% threshold tick */
function TugOfWar({ leftPct, leftLabel, rightLabel, threshPct, threshLabel, state }) {
  const w = 280, h = 16, cw = (leftPct / 100) * w;
  return (
    <svg data-microvisual="tug" width="100%" viewBox={`0 0 ${w} ${h + 26}`} style={{ display: "block" }}>
      <rect x="0" y="0" width={w} height={h} rx="3" fill={UW.faint} opacity="0.5"></rect>
      <rect x="0" y="0" width={cw} height={h} rx="3" fill={state === "green" ? UW.green : UW.red} opacity="0.85"></rect>
      <line x1={w * (threshPct / 100)} y1="-2" x2={w * (threshPct / 100)} y2={h + 2} stroke={UW.text} strokeWidth="1.5" strokeDasharray="2,2"></line>
      <text x={w * (threshPct / 100)} y={h + 12} fontSize="8" fill={UW.dim} textAnchor="middle" fontFamily={FONT_MONO}>{threshLabel}</text>
      <text x="2" y={h + 24} fontSize="9.5" fill={UW.text} fontFamily={FONT_MONO}>{leftLabel}</text>
      <text x={w - 2} y={h + 24} fontSize="9.5" fill={UW.dim} fontFamily={FONT_MONO} textAnchor="end">{rightLabel}</text>
    </svg>
  );
}

/* THE one default-render chart: cumulative net opening premium + a dot per
   qualifying alert (radius ∝ premium). Anchors: "9:30a" / "now · last buy Nm
   ago" / running $ total at the line's end. Colored by the gate's state. */
function FlowSession({ pts, alerts, total, startNote, endNote, state, width = 280, height = 44 }) {
  const max = Math.max(...pts), min = Math.min(0, ...pts);
  const X = (i) => 4 + (i / (pts.length - 1)) * (width - 46);
  const Y = (v) => height - 12 - ((v - min) / (max - min || 1)) * (height - 20);
  const d = pts.map((v, i) => `${i ? "L" : "M"}${X(i)},${Y(v)}`).join(" ");
  const col = stateColor(state);
  return (
    <svg data-flow-strip="true" width="100%" viewBox={`0 0 ${width} ${height + 10}`} style={{ display: "block" }} role="img" aria-label={`Session flow, running total ${total}`}>
      {max > 0 && <rect x="4" y={Y(max)} width={width - 50} height={Math.max(Y(max * 0.9) - Y(max), 2)} fill={UW.green} opacity="0.12"></rect>}
      <path d={d} fill="none" stroke={col} strokeWidth="1.8" strokeLinejoin="round"></path>
      {alerts.map((a, k) => (
        <circle key={k} cx={X(a.i)} cy={Y(pts[a.i])} r={1.8 + a.size * 2.4} fill={col} opacity="0.9"></circle>
      ))}
      <text x={X(pts.length - 1) + 6} y={Y(pts[pts.length - 1]) + 3} fontSize="9.5" fill={UW.text} fontFamily={FONT_MONO}>{total}</text>
      <text x="4" y={height + 8} fontSize="8" fill={UW.dim} fontFamily={FONT_MONO}>{startNote}</text>
      <text x={width - 46} y={height + 8} fontSize="8" fill={UW.dim} fontFamily={FONT_MONO} textAnchor="end">{endNote}</text>
    </svg>
  );
}

/* strike ladder: dot "you are here", flip below, ceiling above, shaded room.
   Optional `bars` (server-sent per-strike net gamma {x, v}) render as faint rungs
   behind the markers — operator amendment 2026-06-13; padding scales with price so
   real dollar geometry doesn't squash (1.2 was tuned for ~$140 fixtures). */
function Ladder({ spot, flip, wall, spotLabel, spotNote, flipLabel, flipNote, wallLabel, wallNote, roomLabel, bars, state }) {
  const w = 280, h = 88, pad = 38;
  const padV = Math.max(1.2, spot * 0.006);
  const lo = Math.min(flip, spot, wall) - padV, hi = Math.max(flip, spot, wall) + padV;
  const X = (p) => pad + ((p - lo) / (hi - lo)) * (w - pad * 2);
  const zoneA = Math.min(X(spot), X(wall)), zoneW = Math.abs(X(wall) - X(spot));
  const rungs = (bars || []).filter((b) => b.x >= lo && b.x <= hi);
  const vMax = Math.max(...rungs.map((b) => Math.abs(b.v)), 0) || 1;
  return (
    <svg data-microvisual="ladder" width="100%" viewBox={`0 0 ${w} ${h}`} style={{ display: "block" }}>
      <rect x={zoneA} y="32" width={zoneW} height="14" fill={state === "green" ? UW.green : UW.gray} opacity="0.14"></rect>
      {rungs.map((b, i) => {
        const bh = Math.max((Math.abs(b.v) / vMax) * 15, 1);
        return <rect key={i} x={X(b.x) - 1.4} y={b.v >= 0 ? 39 - bh : 39} width="2.8" height={bh} fill={b.v >= 0 ? UW.green : UW.red} opacity="0.3"></rect>;
      })}
      <line x1={pad} y1="39" x2={w - pad} y2="39" stroke={UW.faint} strokeWidth="1.5"></line>
      <line x1={X(flip)} y1="26" x2={X(flip)} y2="52" stroke={UW.amber} strokeWidth="1.5"></line>
      <text x={X(flip)} y="63" fontSize="9.5" fill={UW.amber} textAnchor="middle" fontFamily={FONT_MONO}>{flipLabel}</text>
      <text x={X(flip)} y="74" fontSize="8" fill={UW.dim} textAnchor="middle" fontFamily={FONT_BODY}>{flipNote}</text>
      <line x1={X(wall)} y1="26" x2={X(wall)} y2="52" stroke={UW.text} strokeWidth="1.5"></line>
      <text x={X(wall)} y="63" fontSize="9.5" fill={UW.text} textAnchor="middle" fontFamily={FONT_MONO}>{wallLabel}</text>
      <text x={X(wall)} y="74" fontSize="8" fill={UW.dim} textAnchor="middle" fontFamily={FONT_BODY}>{wallNote}</text>
      <circle cx={X(spot)} cy="39" r="5" fill={stateColor(state)}></circle>
      <text x={X(spot)} y="13" fontSize="8" fill={UW.dim} textAnchor="middle" fontFamily={FONT_BODY}>{spotNote}</text>
      <text x={X(spot)} y="23" fontSize="9.5" fill={UW.text} textAnchor="middle" fontFamily={FONT_MONO}>{spotLabel}</text>
      <text x={(X(spot) + X(wall)) / 2} y="86" fontSize="8" fill={state === "green" ? UW.green : UW.dim} textAnchor="middle" fontFamily={FONT_MONO} opacity="0.85">{roomLabel}</text>
    </svg>
  );
}

/* actual-vs-charged paired bars + IV-rank strip with 0–30 green segment */
function CheapVol({ actual, charged, ivRank, actualTitle, actualLabel, chargedTitle, chargedLabel, rankTitle, rankLabel, leftAnchor, rightAnchor, state }) {
  const w = 280, bw = 165, maxV = Math.max(actual, charged);
  const col = stateColor(state);
  const stripW = w - 70;
  return (
    <svg data-microvisual="cheap_vol" width="100%" viewBox={`0 0 ${w} 86`} style={{ display: "block" }}>
      <text x="0" y="9" fontSize="8.5" fill={UW.dim} fontFamily={FONT_BODY}>{actualTitle}</text>
      <rect x="0" y="13" width={(actual / maxV) * bw} height="9" rx="2" fill={col} opacity="0.9"></rect>
      <text x={(actual / maxV) * bw + 6} y="21" fontSize="9.5" fill={UW.text} fontFamily={FONT_MONO}>{actualLabel}</text>
      <text x="0" y="37" fontSize="8.5" fill={UW.dim} fontFamily={FONT_BODY}>{chargedTitle}</text>
      <rect x="0" y="41" width={(charged / maxV) * bw} height="9" rx="2" fill={UW.faint}></rect>
      <text x={(charged / maxV) * bw + 6} y="49" fontSize="9.5" fill={UW.text} fontFamily={FONT_MONO}>{chargedLabel}</text>
      <text x="0" y="68" fontSize="8.5" fill={UW.dim} fontFamily={FONT_BODY}>{rankTitle}</text>
      <rect x="0" y="72" width={stripW} height="5" rx="2.5" fill={UW.faint} opacity="0.4"></rect>
      <rect x="0" y="72" width={stripW * 0.3} height="5" rx="2.5" fill={UW.green} opacity="0.35"></rect>
      <circle cx={stripW * (ivRank / 100)} cy="74.5" r="4" fill={col}></circle>
      <text x={Math.min(stripW * (ivRank / 100) + 8, stripW + 4)} y="78" fontSize="9.5" fill={UW.text} fontFamily={FONT_MONO}>{rankLabel}</text>
      <text x="0" y="85" fontSize="8" fill={UW.dim} fontFamily={FONT_MONO}>{leftAnchor}</text>
      <text x={stripW} y="85" fontSize="8" fill={UW.dim} fontFamily={FONT_MONO} textAnchor="end">{rightAnchor}</text>
    </svg>
  );
}

/* runway: 0% → expected% track, breakeven tick, toll stub, 70% pass zone */
function Runway({ needPct, expectPct, tollPct, passFrac, needLabel, needNote, zeroLabel, expectLabel, state }) {
  const w = 280, trackW = 250;
  const X = (p) => 8 + (p / expectPct) * (trackW - 8);
  const col = stateColor(state);
  return (
    <svg data-microvisual="runway" width="100%" viewBox={`0 0 ${w} 58`} style={{ display: "block" }}>
      <rect x="8" y="20" width={trackW} height="10" rx="5" fill={UW.faint} opacity="0.35"></rect>
      <rect x="8" y="20" width={trackW * passFrac} height="10" rx="5" fill={UW.green} opacity="0.14"></rect>
      <rect x="8" y="20" width={Math.max(trackW * (tollPct / 100), 5)} height="10" fill={UW.faint}></rect>
      <line x1={X(needPct)} y1="13" x2={X(needPct)} y2="37" stroke={col} strokeWidth="2.5"></line>
      <text x={X(needPct)} y="48" fontSize="9.5" fill={UW.text} textAnchor="middle" fontFamily={FONT_MONO}>{needLabel}</text>
      <text x={X(needPct)} y="57" fontSize="8" fill={UW.dim} textAnchor="middle" fontFamily={FONT_BODY}>{needNote}</text>
      <text x="8" y="14" fontSize="8.5" fill={UW.dim} fontFamily={FONT_MONO}>{zeroLabel}</text>
      <text x={8 + trackW} y="14" fontSize="8.5" fill={UW.dim} textAnchor="end" fontFamily={FONT_MONO}>{expectLabel}</text>
    </svg>
  );
}

/* checklist — no chart, deliberately (categorical gate) */
function Checks({ items }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      {items.map(([ok, label]) => (
        <div key={label} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12, color: ok === null ? UW.dim : UW.text, fontFamily: FONT_BODY }}>
          <span aria-hidden="true" style={{ color: ok === null ? UW.gray : ok ? UW.green : UW.red, fontFamily: FONT_MONO, fontSize: 12, width: 12, flexShrink: 0 }}>
            {ok === null ? "·" : ok ? "✓" : "✗"}
          </span>
          <span>{label}</span>
          <span className="visually-hidden">{ok === null ? "no data" : ok ? "clear" : "failed"}</span>
        </div>
      ))}
    </div>
  );
}

/* dot strip: last 8 report moves as dots, one line = the implied move charged now */
function DotStrip({ moves, implied, avg, impliedLabel, impliedNote, avgLabel, dotsNote, state }) {
  const w = 280, maxV = Math.max(...moves, implied) * 1.15;
  const X = (v) => 8 + (v / maxV) * (w - 16);
  const col = stateColor(state);
  return (
    <svg data-microvisual="dot_strip" width="100%" viewBox={`0 0 ${w} 60`} style={{ display: "block" }}>
      <rect x={X(implied)} y="10" width={w - 8 - X(implied)} height="22" fill={UW.green} opacity="0.1"></rect>
      <line x1="8" y1="21" x2={w - 8} y2="21" stroke={UW.faint} strokeWidth="1"></line>
      {moves.map((m, i) => <circle key={i} cx={X(m)} cy="21" r="4" fill={UW.dim} opacity="0.85"></circle>)}
      <line x1={X(avg)} y1="13" x2={X(avg)} y2="29" stroke={UW.dim} strokeWidth="1" strokeDasharray="2,2"></line>
      <text x={X(avg)} y="8" fontSize="8.5" fill={UW.dim} textAnchor="middle" fontFamily={FONT_MONO}>{avgLabel}</text>
      <line x1={X(implied)} y1="6" x2={X(implied)} y2="34" stroke={col} strokeWidth="2.5"></line>
      <text x={X(implied)} y="46" fontSize="9.5" fill={UW.text} textAnchor="middle" fontFamily={FONT_MONO}>{impliedLabel}</text>
      <text x={X(implied)} y="56" fontSize="8" fill={UW.dim} textAnchor="middle" fontFamily={FONT_BODY}>{impliedNote}</text>
      <text x="8" y="56" fontSize="8" fill={UW.dim} fontFamily={FONT_BODY}>{dotsNote}</text>
    </svg>
  );
}

/* DARK gate inside the why panel: no chart, never fabricated */
function DarkNote({ lines }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {lines.map((l) => (
        <div key={l} style={{ fontSize: 12, color: UW.dim, fontFamily: FONT_BODY }}>{l}</div>
      ))}
      <div style={{ fontSize: 10.5, color: UW.gray, fontFamily: FONT_MONO, marginTop: 2 }}>counted against the verdict — never guessed</div>
    </div>
  );
}

Object.assign(window, { TugOfWar, FlowSession, Ladder, CheapVol, Runway, Checks, DotStrip, DarkNote });
