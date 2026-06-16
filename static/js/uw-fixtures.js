/* Fixture ViewModels — extensions of the golden SPY contract (tests/fixtures/
   golden_viewmodel/SPY.json), one per verdict state. Everything the frontend
   renders arrives here pre-formatted: states, counts, waiting lines, captions,
   sub-criterion strings, sparkline points, alert dots. The frontend computes
   geometry only. See "Present Contract Extensions.md" for the present.py spec.

   States covered:
   - NVDA calls  — PERFECT 4/4 (drift) + the four numbers
   - AMD  calls  — NOT NOW 3/4, one RED gate, numbers shown (n ≥ N−1)
   - ORCL puts   — CATALYST branch, earnings tag, DARK no_squeeze (3/4)
   - GME  puts   — no_squeeze HARD VETO red, named first in waiting
   - SPY  puts   — heavy-DARK day, 0/5 (lifted from the golden fixture)        */

const G = {
  smart_flow: "Smart money just opened this bet",
  dealer_fuel: "Dealers will pour fuel on the move",
  cheap_vol: "The options are cheap for how much this moves",
  good_entry: "You're not overpaying to get in",
  no_squeeze: "No squeeze trap",
  cheap_event: "The market is underpricing this report",
};

const NVDA_CALLS = {
  ticker: "NVDA", direction: "CALLS", tag: null, branch: "drift",
  state: "PERFECT", green: 4, total: 4, waiting: null,
  gates: [
    { name: "smart_flow", state: "green", label: G.smart_flow, short: "smart money",
      flow: { pts: [0, 2, 5, 5, 9, 14, 15, 21, 24, 28, 29, 33, 38, 41],
        alerts: [{ i: 1, size: 0.3 }, { i: 4, size: 0.6 }, { i: 7, size: 1 }, { i: 9, size: 0.5 }, { i: 12, size: 0.8 }, { i: 13, size: 0.4 }],
        total: "$4.2M", startNote: "9:30a", endNote: "now · last buy 22m ago" },
      why: { kind: "tug", caption: "6 qualifying buy orders since the open · top 4% of the whole market today",
        subtext: "ask-side 82% (needs \u226570) · $4.2M opening (needs 90th pct) · 2 expiries, 0.25\u20130.55\u0394 · within 4% of session high · live · as of 12:42 PT",
        data: { leftPct: 82, leftLabel: "$4.2M calls (82%)", rightLabel: "$0.9M puts", threshPct: 70, threshLabel: "70% needed" } } },
    { name: "dealer_fuel", state: "green", label: G.dealer_fuel, short: "dealer fuel",
      why: { kind: "ladder", caption: "market makers are forced to buy as it rises until $146 — they amplify, not resist",
        subtext: "GEX \u2212$0.8B (bottom quartile) · spot 1.1% past the flip (needs \u22650.5) · wall 1.4 EM away (needs \u22651) · live · as of 12:42 PT",
        data: { spot: 142.1, flip: 140.6, wall: 146, spotLabel: "$142.10", spotNote: "you are here", flipLabel: "$141", flipNote: "fuel off below", wallLabel: "$146", wallNote: "ceiling", roomLabel: "1.4 expected moves of room" } } },
    { name: "cheap_vol", state: "green", label: G.cheap_vol, short: "cheap options",
      why: { kind: "cheap_vol", caption: "movement costs less than it's been delivering · calendar clear through your hold",
        subtext: "IV rank 22 (needs <30) · HV/IV 1.14 (needs \u22651.0) · term slope +0.3 (needs \u22650) · no events 3d · live · as of 12:42 PT",
        data: { actual: 2.4, charged: 2.1, ivRank: 22, actualTitle: "how much it actually moves (5-day)", actualLabel: "2.4%/day", chargedTitle: "what the options charge (this week)", chargedLabel: "2.1%/day", rankTitle: "option price vs its past year", rankLabel: "22/100", leftAnchor: "cheapest \u2190", rightAnchor: "\u2192 priciest" } } },
    { name: "good_entry", state: "green", label: G.good_entry, short: "entry cost",
      why: { kind: "runway", caption: "gray stub at the start = the 2.6% entry toll, already counted in your breakeven",
        subtext: "spread 2.6% (tier cap 3%) · breakeven 61% of expected (needs \u226470) · \u03b8 7%/day (cap 10) · \u03940.48 · live · as of 12:42 PT",
        data: { needPct: 1.1, expectPct: 1.8, tollPct: 2.6, passFrac: 0.7, needLabel: "+1.1%", needNote: "break even", zeroLabel: "0%", expectLabel: "+1.8% expected" } } },
  ],
  numbers: [
    ["Entry toll", "2.6% of ticket"],
    ["Needs vs expects", "+1.1% vs +1.8%"],
    ["Time stop", "3 days"],
    ["Contract / max loss", "$144C 6/26 · $385"],
  ],
};

const NVDA_PUTS = {
  ticker: "NVDA", direction: "PUTS", tag: null, branch: "drift",
  state: "NOT NOW", green: 3, total: 5, waiting: "Waiting on: smart money, dealer fuel",
  gates: [
    { name: "smart_flow", state: "red", label: G.smart_flow, short: "smart money",
      flow: { pts: [0, 1, 2, 2, 3, 3, 4, 4, 4, 5, 5, 5, 5, 5],
        alerts: [{ i: 2, size: 0.3 }, { i: 6, size: 0.4 }],
        total: "$0.9M", startNote: "9:30a", endNote: "now · last buy 96m ago" },
      why: { kind: "tug", caption: "the money is on the other side today — puts are 18% of the opening premium",
        subtext: "ask-side 18% (needs \u226570) · last print 96m ago (needs \u226490) · live · as of 12:42 PT",
        data: { leftPct: 18, leftLabel: "$0.9M puts (18%)", rightLabel: "$4.2M calls", threshPct: 70, threshLabel: "70% needed" } } },
    { name: "dealer_fuel", state: "red", label: G.dealer_fuel, short: "dealer fuel",
      why: { kind: "ladder", caption: "the amplification zone runs upward from here — dealers would resist a drop, not fuel it",
        subtext: "spot above the flip on the put side (needs \u22650.5% below) · live · as of 12:42 PT",
        data: { spot: 142.1, flip: 140.6, wall: 137.8, spotLabel: "$142.10", spotNote: "you are here", flipLabel: "$141", flipNote: "fuel starts below", wallLabel: "$138", wallNote: "floor", roomLabel: "fuel is on the call side today" } } },
    { name: "cheap_vol", state: "green", label: G.cheap_vol, short: "cheap options",
      why: { kind: "cheap_vol", caption: "movement costs less than it's been delivering · calendar clear through your hold",
        subtext: "IV rank 22 (needs <30) · HV/IV 1.14 (needs \u22651.0) · live · as of 12:42 PT",
        data: { actual: 2.4, charged: 2.1, ivRank: 22, actualTitle: "how much it actually moves (5-day)", actualLabel: "2.4%/day", chargedTitle: "what the options charge (this week)", chargedLabel: "2.1%/day", rankTitle: "option price vs its past year", rankLabel: "22/100", leftAnchor: "cheapest \u2190", rightAnchor: "\u2192 priciest" } } },
    { name: "good_entry", state: "green", label: G.good_entry, short: "entry cost",
      why: { kind: "runway", caption: "entry itself is fine — the bet just isn't there",
        subtext: "spread 2.9% (tier cap 3%) · breakeven 66% of expected (needs \u226470) · live · as of 12:42 PT",
        data: { needPct: 1.2, expectPct: 1.8, tollPct: 2.9, passFrac: 0.7, needLabel: "\u22121.2%", needNote: "break even", zeroLabel: "0%", expectLabel: "\u22121.8% expected" } } },
    { name: "no_squeeze", state: "green", label: G.no_squeeze, short: "squeeze check",
      why: { caption: "no trap conditions on the short side",
        subtext: "SI 2.1% float (cap 10) · FTDs unremarkable · front IV +3% 2d (cap +20) · live · as of 12:42 PT",
        items: [[true, "Not crowded by short sellers"], [true, "No delivery failures piling up"], [true, "No panic premium in the last 2 days"]] } },
  ],
  numbers: null,
};

const AMD_CALLS = {
  ticker: "AMD", direction: "CALLS", tag: null, branch: "drift",
  state: "NOT NOW", green: 3, total: 4, waiting: "Waiting on: cheap options",
  gates: [
    { name: "smart_flow", state: "green", label: G.smart_flow, short: "smart money",
      flow: { pts: [0, 3, 6, 10, 11, 16, 19, 22, 26, 27, 31, 33, 34, 36],
        alerts: [{ i: 2, size: 0.5 }, { i: 5, size: 0.7 }, { i: 8, size: 0.4 }, { i: 11, size: 0.6 }],
        total: "$1.8M", startNote: "9:30a", endNote: "now · last buy 41m ago" },
      why: { kind: "tug", caption: "4 qualifying buy orders · top 8% of the market today",
        subtext: "ask-side 80% (needs \u226570) · $1.8M opening (needs 90th pct) · within 6% of session high · live · as of 12:42 PT",
        data: { leftPct: 80, leftLabel: "$1.8M calls (80%)", rightLabel: "$0.4M puts", threshPct: 70, threshLabel: "70% needed" } } },
    { name: "dealer_fuel", state: "green", label: G.dealer_fuel, short: "dealer fuel",
      why: { kind: "ladder", caption: "amplification zone runs from here to $123",
        subtext: "GEX \u2212$0.3B (bottom quartile) · spot 1.0% past the flip · wall 1.6 EM away · live · as of 12:42 PT",
        data: { spot: 118.4, flip: 117.2, wall: 123, spotLabel: "$118.40", spotNote: "you are here", flipLabel: "$117", flipNote: "fuel off below", wallLabel: "$123", wallNote: "ceiling", roomLabel: "1.6 expected moves of room" } } },
    { name: "cheap_vol", state: "red", label: G.cheap_vol, short: "cheap options",
      why: { kind: "cheap_vol", caption: "charging 2.9%/day of movement, delivering 1.6% — you'd pay for motion that isn't happening",
        subtext: "IV rank 64 (needs <30) · HV/IV 0.55 (needs \u22651.0) · live · as of 12:42 PT",
        data: { actual: 1.6, charged: 2.9, ivRank: 64, actualTitle: "how much it actually moves (5-day)", actualLabel: "1.6%/day", chargedTitle: "what the options charge (this week)", chargedLabel: "2.9%/day", rankTitle: "option price vs its past year", rankLabel: "64/100", leftAnchor: "cheapest \u2190", rightAnchor: "\u2192 priciest" } } },
    { name: "good_entry", state: "green", label: G.good_entry, short: "entry cost",
      why: { kind: "runway", caption: "entry itself is fine — the problem is the gate above",
        subtext: "spread 3.4% (tier cap 5%) · breakeven 60% of expected (needs \u226470) · \u03b8 9%/day (cap 10) · live · as of 12:42 PT",
        data: { needPct: 1.2, expectPct: 2.0, tollPct: 3.4, passFrac: 0.7, needLabel: "+1.2%", needNote: "break even", zeroLabel: "0%", expectLabel: "+2.0% expected" } } },
  ],
  numbers: [
    ["Entry toll", "3.4% of ticket"],
    ["Needs vs expects", "+1.2% vs +2.0%"],
    ["Time stop", "3 days"],
    ["Contract / max loss", "$120C 6/26 · $310"],
  ],
};

const AMD_PUTS = {
  ticker: "AMD", direction: "PUTS", tag: null, branch: "drift",
  state: "NOT NOW", green: 2, total: 5, waiting: "Waiting on: smart money, dealer fuel, cheap options",
  gates: [
    { name: "smart_flow", state: "red", label: G.smart_flow, short: "smart money",
      flow: { pts: [0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3, 3, 4, 4],
        alerts: [{ i: 5, size: 0.3 }],
        total: "$0.4M", startNote: "9:30a", endNote: "now · last buy 118m ago" },
      why: { kind: "tug", caption: "the money is betting up, not down",
        subtext: "ask-side 20% (needs \u226570) · $0.4M opening (needs 90th pct) · live · as of 12:42 PT",
        data: { leftPct: 20, leftLabel: "$0.4M puts (20%)", rightLabel: "$1.8M calls", threshPct: 70, threshLabel: "70% needed" } } },
    { name: "dealer_fuel", state: "red", label: G.dealer_fuel, short: "dealer fuel",
      why: { kind: "ladder", caption: "dealers would resist a drop here",
        subtext: "spot above the flip on the put side · live · as of 12:42 PT",
        data: { spot: 118.4, flip: 117.2, wall: 114.5, spotLabel: "$118.40", spotNote: "you are here", flipLabel: "$117", flipNote: "fuel starts below", wallLabel: "$114", wallNote: "floor", roomLabel: "fuel is on the call side today" } } },
    { name: "cheap_vol", state: "red", label: G.cheap_vol, short: "cheap options",
      why: { kind: "cheap_vol", caption: "same problem both directions — the options are overpriced for the movement",
        subtext: "IV rank 64 (needs <30) · HV/IV 0.55 (needs \u22651.0) · live · as of 12:42 PT",
        data: { actual: 1.6, charged: 2.9, ivRank: 64, actualTitle: "how much it actually moves (5-day)", actualLabel: "1.6%/day", chargedTitle: "what the options charge (this week)", chargedLabel: "2.9%/day", rankTitle: "option price vs its past year", rankLabel: "64/100", leftAnchor: "cheapest \u2190", rightAnchor: "\u2192 priciest" } } },
    { name: "good_entry", state: "green", label: G.good_entry, short: "entry cost",
      why: { kind: "runway", caption: "entry is fair — everything upstream is not",
        subtext: "spread 3.8% (tier cap 5%) · breakeven 64% of expected · live · as of 12:42 PT",
        data: { needPct: 1.3, expectPct: 2.0, tollPct: 3.8, passFrac: 0.7, needLabel: "\u22121.3%", needNote: "break even", zeroLabel: "0%", expectLabel: "\u22122.0% expected" } } },
    { name: "no_squeeze", state: "green", label: G.no_squeeze, short: "squeeze check",
      why: { caption: "no trap conditions on the short side",
        subtext: "SI 3.4% float (cap 10) · FTDs unremarkable · live · as of 12:42 PT",
        items: [[true, "Not crowded by short sellers"], [true, "No delivery failures piling up"], [true, "No panic premium in the last 2 days"]] } },
  ],
  numbers: null,
};

const ORCL_PUTS = {
  ticker: "ORCL", direction: "PUTS", tag: "EARNINGS THU", branch: "catalyst",
  state: "NOT NOW", green: 3, total: 4, waiting: "Waiting on: squeeze check",
  gates: [
    { name: "cheap_event", state: "green", label: G.cheap_event, short: "report price",
      why: { kind: "dot_strip", caption: "market charges ±6.1% · the stock's own history says reports move it 8.9% on average",
        subtext: "implied 6.1% \u2264 0.9 \u00d7 8Q mean 8.9% · expiry 6/13 (first weekly after) · live · as of 12:42 PT",
        data: { moves: [7.2, 11.4, 5.9, 13.1, 8.3, 9.6, 6.8, 8.9], implied: 6.1, avg: 8.9, impliedLabel: "\u00b16.1%", impliedNote: "price of this report", avgLabel: "avg 8.9%", dotsNote: "\u25cf last 8 report moves" } } },
    { name: "smart_flow", state: "green", label: G.smart_flow, short: "smart money",
      flow: { pts: [0, 1, 4, 7, 8, 13, 17, 18, 23, 27, 30, 35, 37, 40],
        alerts: [{ i: 3, size: 0.4 }, { i: 6, size: 0.9 }, { i: 9, size: 0.5 }, { i: 11, size: 0.7 }, { i: 13, size: 0.5 }],
        total: "$2.7M", startNote: "9:30a", endNote: "now · last buy 14m ago" },
      why: { kind: "tug", caption: "5 qualifying buy orders on the put side · top 5% of the market today",
        subtext: "ask-side 82% (needs \u226570) · $2.7M opening (needs 90th pct) · within 3% of session high · live · as of 12:42 PT",
        data: { leftPct: 82, leftLabel: "$2.7M puts (82%)", rightLabel: "$0.6M calls", threshPct: 70, threshLabel: "70% needed" } } },
    { name: "good_entry", state: "green", label: G.good_entry, short: "entry cost",
      why: { kind: "runway", caption: "exit on report day, before the post-report price deflation",
        subtext: "spread 4.1% (tier cap 5%) · breakeven 62% of expected (needs \u226470) · live · as of 12:42 PT",
        data: { needPct: 3.8, expectPct: 6.1, tollPct: 4.1, passFrac: 0.7, needLabel: "\u22123.8%", needNote: "break even", zeroLabel: "0%", expectLabel: "\u22126.1% expected" } } },
    { name: "no_squeeze", state: "dark", label: G.no_squeeze, short: "squeeze check",
      why: { caption: "short-seller crowding unreadable on this data plan — counted against the verdict, never guessed",
        subtext: "interest-float unavailable · derived · no as_of",
        items: [[true, "No delivery failures piling up"], [true, "No panic premium in the last 2 days"], [null, "Short-seller crowding — no data"]] } },
  ],
  numbers: [
    ["Entry toll", "4.1% of ticket"],
    ["Needs vs expects", "\u22123.8% vs \u22126.1%"],
    ["Time stop", "exit on report day"],
    ["Contract / max loss", "$215P 6/13 · $410"],
  ],
};

const ORCL_CALLS = {
  ticker: "ORCL", direction: "CALLS", tag: "EARNINGS THU", branch: "catalyst",
  state: "NOT NOW", green: 2, total: 3, waiting: "Waiting on: smart money",
  gates: [
    { name: "cheap_event", state: "green", label: G.cheap_event, short: "report price",
      why: { kind: "dot_strip", caption: "the report is cheap either direction — but the money is betting down",
        subtext: "implied 6.1% \u2264 0.9 \u00d7 8Q mean 8.9% · live · as of 12:42 PT",
        data: { moves: [7.2, 11.4, 5.9, 13.1, 8.3, 9.6, 6.8, 8.9], implied: 6.1, avg: 8.9, impliedLabel: "\u00b16.1%", impliedNote: "price of this report", avgLabel: "avg 8.9%", dotsNote: "\u25cf last 8 report moves" } } },
    { name: "smart_flow", state: "red", label: G.smart_flow, short: "smart money",
      flow: { pts: [0, 0, 1, 1, 2, 2, 2, 3, 3, 4, 4, 5, 5, 6],
        alerts: [{ i: 4, size: 0.3 }, { i: 11, size: 0.4 }],
        total: "$0.6M", startNote: "9:30a", endNote: "now · last buy 63m ago" },
      why: { kind: "tug", caption: "the opening money is 82% on the put side",
        subtext: "ask-side 18% (needs \u226570) · live · as of 12:42 PT",
        data: { leftPct: 18, leftLabel: "$0.6M calls (18%)", rightLabel: "$2.7M puts", threshPct: 70, threshLabel: "70% needed" } } },
    { name: "good_entry", state: "green", label: G.good_entry, short: "entry cost",
      why: { kind: "runway", caption: "entry is fair on this side too",
        subtext: "spread 3.9% (tier cap 5%) · breakeven 64% of expected · live · as of 12:42 PT",
        data: { needPct: 3.9, expectPct: 6.1, tollPct: 3.9, passFrac: 0.7, needLabel: "+3.9%", needNote: "break even", zeroLabel: "0%", expectLabel: "+6.1% expected" } } },
  ],
  numbers: [
    ["Entry toll", "3.9% of ticket"],
    ["Needs vs expects", "+3.9% vs +6.1%"],
    ["Time stop", "exit on report day"],
    ["Contract / max loss", "$235C 6/13 · $395"],
  ],
};

const GME_PUTS = {
  ticker: "GME", direction: "PUTS", tag: null, branch: "drift",
  state: "NOT NOW", green: 3, total: 5, waiting: "Waiting on: squeeze check, cheap options",
  gates: [
    { name: "smart_flow", state: "green", label: G.smart_flow, short: "smart money",
      flow: { pts: [0, 4, 9, 11, 15, 22, 24, 30, 33, 36, 41, 44, 47, 51],
        alerts: [{ i: 1, size: 0.6 }, { i: 5, size: 1 }, { i: 7, size: 0.5 }, { i: 10, size: 0.7 }, { i: 13, size: 0.6 }],
        total: "$5.1M", startNote: "9:30a", endNote: "now · last buy 9m ago" },
      why: { kind: "tug", caption: "heavy, fresh put buying — but read the squeeze gate before believing it",
        subtext: "ask-side 78% (needs \u226570) · $5.1M opening (needs 90th pct) · within 2% of session high · live · as of 12:42 PT",
        data: { leftPct: 78, leftLabel: "$5.1M puts (78%)", rightLabel: "$1.4M calls", threshPct: 70, threshLabel: "70% needed" } } },
    { name: "dealer_fuel", state: "green", label: G.dealer_fuel, short: "dealer fuel",
      why: { kind: "ladder", caption: "dealers amplify a drop from here to $21",
        subtext: "GEX \u2212$0.1B (bottom quartile) · spot 0.8% past the flip · floor 1.2 EM away · live · as of 12:42 PT",
        data: { spot: 24.6, flip: 25.1, wall: 21.2, spotLabel: "$24.60", spotNote: "you are here", flipLabel: "$25", flipNote: "fuel off above", wallLabel: "$21", wallNote: "floor", roomLabel: "1.2 expected moves of room" } } },
    { name: "cheap_vol", state: "red", label: G.cheap_vol, short: "cheap options",
      why: { kind: "cheap_vol", caption: "everyone already paid up for this move — you'd be buying panic",
        subtext: "IV rank 88 (needs <30) · HV/IV 0.7 (needs \u22651.0) · live · as of 12:42 PT",
        data: { actual: 4.1, charged: 5.9, ivRank: 88, actualTitle: "how much it actually moves (5-day)", actualLabel: "4.1%/day", chargedTitle: "what the options charge (this week)", chargedLabel: "5.9%/day", rankTitle: "option price vs its past year", rankLabel: "88/100", leftAnchor: "cheapest \u2190", rightAnchor: "\u2192 priciest" } } },
    { name: "good_entry", state: "green", label: G.good_entry, short: "entry cost",
      why: { kind: "runway", caption: "entry is workable — the trap is upstream",
        subtext: "spread 4.6% (tier cap 5%) · breakeven 68% of expected · live · as of 12:42 PT",
        data: { needPct: 4.2, expectPct: 6.4, tollPct: 4.6, passFrac: 0.7, needLabel: "\u22124.2%", needNote: "break even", zeroLabel: "0%", expectLabel: "\u22126.4% expected" } } },
    { name: "no_squeeze", state: "red", label: G.no_squeeze, short: "squeeze check",
      why: { caption: "betting on a drop in a crowded short is how squeezes eat puts — this is a hard veto",
        subtext: "SI 24% float (cap 10) · front IV +31% in 2 sessions (cap +20) · live · as of 12:42 PT",
        items: [[false, "Crowded by short sellers — 24% of the float"], [false, "Panic premium — option prices up 31% in 2 days"], [true, "No delivery failures piling up"]] } },
  ],
  numbers: null,
};

const GME_CALLS = {
  ticker: "GME", direction: "CALLS", tag: null, branch: "drift",
  state: "NOT NOW", green: 1, total: 4, waiting: "Waiting on: smart money, dealer fuel, cheap options",
  gates: [
    { name: "smart_flow", state: "red", label: G.smart_flow, short: "smart money",
      flow: { pts: [0, 1, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 13, 14],
        alerts: [{ i: 3, size: 0.4 }, { i: 8, size: 0.5 }],
        total: "$1.4M", startNote: "9:30a", endNote: "now · last buy 52m ago" },
      why: { kind: "tug", caption: "the heavy money is on the put side today",
        subtext: "ask-side 22% (needs \u226570) · live · as of 12:42 PT",
        data: { leftPct: 22, leftLabel: "$1.4M calls (22%)", rightLabel: "$5.1M puts", threshPct: 70, threshLabel: "70% needed" } } },
    { name: "dealer_fuel", state: "red", label: G.dealer_fuel, short: "dealer fuel",
      why: { kind: "ladder", caption: "the amplification zone points down, not up",
        subtext: "spot below the flip on the call side (needs \u22650.5% above) · live · as of 12:42 PT",
        data: { spot: 24.6, flip: 25.1, wall: 27.4, spotLabel: "$24.60", spotNote: "you are here", flipLabel: "$25", flipNote: "fuel on above", wallLabel: "$27", wallNote: "ceiling", roomLabel: "fuel is on the put side today" } } },
    { name: "cheap_vol", state: "red", label: G.cheap_vol, short: "cheap options",
      why: { kind: "cheap_vol", caption: "overpriced in both directions",
        subtext: "IV rank 88 (needs <30) · live · as of 12:42 PT",
        data: { actual: 4.1, charged: 5.9, ivRank: 88, actualTitle: "how much it actually moves (5-day)", actualLabel: "4.1%/day", chargedTitle: "what the options charge (this week)", chargedLabel: "5.9%/day", rankTitle: "option price vs its past year", rankLabel: "88/100", leftAnchor: "cheapest \u2190", rightAnchor: "\u2192 priciest" } } },
    { name: "good_entry", state: "green", label: G.good_entry, short: "entry cost",
      why: { kind: "runway", caption: "entry is workable on this side too",
        subtext: "spread 4.9% (tier cap 5%) · live · as of 12:42 PT",
        data: { needPct: 4.4, expectPct: 6.4, tollPct: 4.9, passFrac: 0.7, needLabel: "+4.4%", needNote: "break even", zeroLabel: "0%", expectLabel: "+6.4% expected" } } },
  ],
  numbers: null,
};

/* Lifted from tests/fixtures/golden_viewmodel/SPY.json — the heavy-DARK day.
   spark downsampled from the golden 49-point series ($M); waiting string verbatim. */
const SPY_PUTS = {
  ticker: "SPY", direction: "PUTS", tag: null, branch: "drift",
  state: "NOT NOW", green: 0, total: 5,
  waiting: "Waiting on: smart-money flow, a fair entry, squeeze risk cleared, dealer fuel, cheap options",
  gates: [
    { name: "smart_flow", state: "red", label: G.smart_flow, short: "smart money",
      flow: { pts: [-0.2, 0.6, 1.5, 1.2, 1.6, 1.9, 1.6, 1.4, 1.2, 0.9, 0.5, 1.4, 4.2, 5.1, 4.7, 4.1, 3.4, 3.0],
        alerts: [{ i: 2, size: 0.5 }, { i: 5, size: 0.4 }, { i: 12, size: 1 }, { i: 13, size: 0.6 }],
        total: "$3.0M", startNote: "9:30a", endNote: "now · last buy 5m ago" },
      why: { kind: "tug", caption: "a put lean, but thin and fading — the morning burst didn't hold",
        subtext: "ask-side 35% (needs \u226570) · lean 1.5:1 weak (needs 2:1) · net 58% of session high (needs \u226590) · archive · as of 15:05 UTC",
        data: { leftPct: 60, leftLabel: "$9.2M puts (60%)", rightLabel: "$6.2M calls", threshPct: 70, threshLabel: "70% needed" } } },
    { name: "dealer_fuel", state: "dark", label: G.dealer_fuel, short: "dealer fuel",
      why: { caption: null, subtext: "derived · no as_of",
        missing: ["dealer positioning feed didn't return this cycle", "no spot-exposures data"] } },
    { name: "cheap_vol", state: "dark", label: G.cheap_vol, short: "cheap options",
      why: { caption: null, subtext: "derived · no as_of",
        missing: ["IV rank unavailable", "realized vol unavailable", "no term structure", "calendar fetch failed"] } },
    { name: "good_entry", state: "dark", label: G.good_entry, short: "entry cost",
      why: { caption: null, subtext: "derived · no as_of",
        missing: ["no chain to price a contract"] } },
    { name: "no_squeeze", state: "dark", label: G.no_squeeze, short: "squeeze check",
      why: { caption: "unreadable this cycle — counted against the verdict, never guessed",
        subtext: "derived · no as_of",
        items: [[null, "Short-seller crowding — no data"], [null, "Delivery failures — no FTD history"], [null, "Panic premium — no IV history"]] } },
  ],
  numbers: null,
};

const SPY_CALLS = {
  ticker: "SPY", direction: "CALLS", tag: null, branch: "drift",
  state: "NOT NOW", green: 0, total: 4,
  waiting: "Waiting on: smart-money flow, a fair entry, dealer fuel, cheap options",
  gates: [
    { name: "smart_flow", state: "red", label: G.smart_flow, short: "smart money",
      flow: { pts: [0.2, 0.7, 1.0, 1.3, 1.5, 1.6, 1.9, 2.2, 2.4, 2.8, 3.1, 3.7, 3.7, 3.8, 4.1, 4.6, 5.4, 6.2],
        alerts: [{ i: 5, size: 0.7 }, { i: 9, size: 0.4 }, { i: 15, size: 0.5 }],
        total: "$6.2M", startNote: "9:30a", endNote: "now · last buy 0m ago" },
      why: { kind: "tug", caption: "calls are the minority side of a weak two-way tape",
        subtext: "ask-side 15% (needs \u226570) · archive · as of 15:05 UTC",
        data: { leftPct: 40, leftLabel: "$6.2M calls (40%)", rightLabel: "$9.2M puts", threshPct: 70, threshLabel: "70% needed" } } },
    { name: "dealer_fuel", state: "dark", label: G.dealer_fuel, short: "dealer fuel",
      why: { caption: null, subtext: "derived · no as_of", missing: ["no spot-exposures data"] } },
    { name: "cheap_vol", state: "dark", label: G.cheap_vol, short: "cheap options",
      why: { caption: null, subtext: "derived · no as_of", missing: ["IV rank unavailable", "realized vol unavailable", "no term structure"] } },
    { name: "good_entry", state: "dark", label: G.good_entry, short: "entry cost",
      why: { caption: null, subtext: "derived · no as_of", missing: ["no contract priced for this side"] } },
  ],
  numbers: null,
};

const UW_TICKERS = {
  NVDA: { ticker: "NVDA", best: "calls", calls: NVDA_CALLS, puts: NVDA_PUTS },
  ORCL: { ticker: "ORCL", best: "puts", calls: ORCL_CALLS, puts: ORCL_PUTS },
  AMD: { ticker: "AMD", best: "calls", calls: AMD_CALLS, puts: AMD_PUTS },
  GME: { ticker: "GME", best: "puts", calls: GME_CALLS, puts: GME_PUTS },
  SPY: { ticker: "SPY", best: "puts", calls: SPY_CALLS, puts: SPY_PUTS },
};

/* Landing grid VM — ticker + best direction + n/N, sorted n desc, PERFECT pinned top */
const UW_GRID = {
  asOf: "12:42 PT",
  status: "Swept 18 names · refreshed 12:42 PT · next sweep 13:00",
  rows: [
    { ticker: "NVDA", direction: "CALLS", state: "PERFECT", green: 4, total: 4, tag: null, sub: "$1.2B prem · $0.8B c / $0.4B p · P/C 0.51" },
    { ticker: "ORCL", direction: "PUTS", state: "NOT NOW", green: 3, total: 4, tag: "EARNINGS THU", sub: "$0.6B prem · $0.2B c / $0.4B p · P/C 1.84" },
    { ticker: "AMD", direction: "CALLS", state: "NOT NOW", green: 3, total: 4, tag: null, sub: "$0.4B prem · $0.3B c / $0.1B p · P/C 0.42" },
    { ticker: "GME", direction: "PUTS", state: "NOT NOW", green: 3, total: 5, tag: null, sub: "$0.2B prem · $0.05B c / $0.15B p · P/C 2.10" },
    { ticker: "SPY", direction: "PUTS", state: "NOT NOW", green: 0, total: 5, tag: null, sub: "$2.7B prem · $1.8B c / $0.9B p · P/C 0.99" },
  ],
};

/* Scanner-in-progress VM — the empty screen says what it's doing */
const UW_SCANNING = {
  asOf: "12:42 PT",
  headline: "SWEEPING THE PANEL",
  progress: "7 of 18 names checked",
  detail: "flow alerts first, then dealer positioning, then the options chain",
  note: "verdicts appear here as each name clears",
};

/* No-candidates VM — an invitation to act, not a dead end */
const UW_EMPTY = {
  asOf: "12:42 PT",
  headline: "NOT NOW — ALL 18 NAMES",
  body: "Swept the full panel at 12:42 PT. No name has every condition in place, which is the normal state of this product.",
  closest: { label: "Closest: AMD · CALLS · 3/4 — waiting on cheap options", ticker: "AMD" },
  next: "Next sweep 13:00 PT · the scanner runs all session",
};

Object.assign(window, { UW_TICKERS, UW_GRID, UW_SCANNING, UW_EMPTY });
