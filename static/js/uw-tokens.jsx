/* Design tokens — lifted verbatim from verdict-mockup.jsx (launch-control annunciator). */
const UW = {
  bg: "#14181D", card: "#1B2128", cardEdge: "#262E37", inset: "#161B21",
  text: "#D8DEE5", dim: "#7B8794", faint: "#4A545F",
  green: "#3DD68C", red: "#E5534B", gray: "#5A646F", amber: "#E8B33C",
};
const FONT_HEAD = "'Chakra Petch', sans-serif";
const FONT_MONO = "'IBM Plex Mono', monospace";
const FONT_BODY = "'IBM Plex Sans', sans-serif";

const stateColor = (s) => (s === "green" ? UW.green : s === "red" ? UW.red : UW.gray);

window.UW_T = { UW, FONT_HEAD, FONT_MONO, FONT_BODY, stateColor };
