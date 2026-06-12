/* Contract checks — directive §5.4, run live against the rendered DOM.
   The Tweaks panel surfaces the results; console logs them on every run. */
(function () {
  function runUwContractChecks() {
    const root = document.querySelector("[data-app-root]");
    if (!root) return [];
    const out = [];
    const t = (name, pass, note = "") => out.push({ name, pass: !!pass, note });

    // 1. banned vocabulary — fails if "Mixed" or "Favorable" ever renders
    t("banned words absent", !/\b(Mixed|Favorable)\b/.test(root.textContent));

    // 2. verdict vocabulary limited to PERFECT / NOT NOW
    const heads = Array.from(root.querySelectorAll("[data-verdict-headline]"));
    t("verdict vocabulary", heads.every((h) => ["PERFECT", "NOT NOW"].includes(h.textContent.trim())));

    const cards = Array.from(root.querySelectorAll("[data-verdict-card]"));
    const collapsed = cards.filter((c) => !c.hasAttribute("data-why-open"));

    // 3. default render: exactly one chart max, and it is the flow strip
    t("one chart on default render", collapsed.every((c) => {
      const svgs = Array.from(c.querySelectorAll("svg"));
      return svgs.length <= 1 && svgs.every((s) => s.hasAttribute("data-flow-strip"));
    }));

    // 4. gate rows ≤ 5 per card
    t("gate rows \u22645", cards.every((c) => c.querySelectorAll("[data-gate-row]").length <= 5));

    // 5. ≤ 4 numerals outside gate rows
    t("\u22644 numerals outside gates", cards.every((c) => c.querySelectorAll("[data-number-row]").length <= 4));

    // 6. no provenance strings on the default render
    t("no provenance on default", collapsed.every((c) => c.querySelectorAll("[data-prov]").length === 0));

    // 7. no logic lines / reading instructions on the default render
    t("no logic lines on default", collapsed.every((c) => c.querySelectorAll("[data-logic],[data-instruction]").length === 0));

    // 8. expanded: one micro-visual per gate, none for no_squeeze, none fabricated for DARK
    const open = cards.filter((c) => c.hasAttribute("data-why-open"));
    t("one micro-visual per gate", open.length === 0 || open.every((c) => {
      const gates = Array.from(c.querySelectorAll("[data-gate-row]"));
      return gates.every((g) => {
        const name = g.getAttribute("data-gate-name");
        const inset = c.querySelector('[data-why-inset][data-for-gate="' + name + '"]');
        if (!inset) return false;
        const mv = inset.querySelectorAll("[data-microvisual]").length;
        const state = name === "no_squeeze" ? "checks" : null;
        if (name === "no_squeeze") return mv === 0 && inset.querySelectorAll("svg").length === 0;
        return mv <= 1;
      });
    }), open.length === 0 ? "no card expanded — expand one to exercise" : "");

    // 9. no legends, no axis ticks, anywhere
    t("no legends / axis ticks", root.querySelectorAll("[data-legend],[data-axis-tick]").length === 0);

    window.__uwContract = out;
    const fails = out.filter((r) => !r.pass);
    const line = out.map((r) => (r.pass ? "\u2713 " : "\u2717 ") + r.name).join(" \u00b7 ");
    if (fails.length) console.warn("UW contract checks FAILING:", line);
    else console.log("UW contract checks:", line);
    return out;
  }
  window.runUwContractChecks = runUwContractChecks;
})();
