# Overshoot / Trend-Carry (OS core)

Pine Script v6 indicator — **OS core** (Overshoot-only build, regime logic stripped from OS-TC v8).

## Summary

**KEPT:**
- Overshoot oscillator: selectable decontaminated anchor (regression / SMA / EMA), lagged-ATR normalization, `os = (close - reg) / atr_os`.
- Dual-gate extremity: percentile tail (`pct_thresh`; basis = pooled OR sign-conditioned) AND absolute ATR floor (`os_min_atr`).
- FADE signals: up-extreme → SHORT, down-extreme → LONG (pure mean-reversion).
- Reversion latch (arm on extreme, fire once as the overshoot contracts back).
- Carry / displacement line: `tc = (reg - baseline) / atr_os`; baseline = VWAP / SMA / EMA (selectable).
- Scout edge table (Fade Long / Fade Short vs baseline), uniform repaint policy.

**STRIPPED (was v8):** structural regime (direction/CHoCH/clean), retracement PB, cascade breaker/BLOWOUT, swings, slope/ER diagnostics, wick channel, regime tint, companion bar-tint dependency. No regime = no PB/SP/BO/structure.

## Source

```pine
//@version=6
indicator("Overshoot / Trend-Carry (OS core)", shorttitle="OS core", overlay=false)

// =====================================================================
// OVERSHOOT-ONLY build (derived from OS-TC v8, regime logic stripped).
// KEPT:
//   - Overshoot oscillator: selectable decontaminated anchor (regression / SMA / EMA),
//     lagged-ATR normalization, os = (close - reg) / atr_os.
//   - Dual-gate extremity: percentile tail (pct_thresh; basis = pooled OR sign-conditioned) AND absolute ATR floor (os_min_atr).
//   - FADE signals: up-extreme -> SHORT, down-extreme -> LONG (pure mean-reversion).
//   - Reversion latch (arm on extreme, fire once as the overshoot contracts back).
//   - Carry / displacement line: tc = (reg - baseline)/atr_os; baseline = VWAP / SMA / EMA (selectable).
//   - Scout edge table (Fade Long / Fade Short vs baseline), uniform repaint policy.
// STRIPPED (was v8): structural regime (direction/CHoCH/clean), retracement PB,
//   cascade breaker/BLOWOUT, swings, slope/ER diagnostics, wick channel, regime tint,
//   companion bar-tint dependency. No regime = no PB/SP/BO/structure.
// =====================================================================

// ===== Inputs =====
// --- Arrows ---
use_reversion = input.bool(false, "Fire on reversion (1 per episode)", group="Arrows", tooltip="ON: arm on the extreme bar, fire ONCE as the overshoot starts contracting back toward the line. OFF (default): every extreme bar fires (scout n counts BARS).")
arm_max_bars  = input.int(24, "Reversion arm timeout (bars)", minval=1, group="Arrows", tooltip="When reversion is on: an armed setup self-clears if it hasn't fired within this many bars of the extreme, so a stale arm can't fire long after the move.")

// --- Display ---
show_turns       = input.bool(true,  "Show fade arrows on price", group="Display")
show_provisional = input.bool(true,  "Show provisional (forming-bar) signals", group="Display", tooltip="ON: the live forming bar's signal renders faint and solidifies on close. OFF: signals appear only on bar close. Committed marks never repaint; alerts fire on close only.")
show_table       = input.bool(true,  "Show state panel (top-right)", group="Display")
show_scout       = input.bool(true,  "Show scout dashboard (bottom-right)", group="Display", tooltip="Descriptive MFE / MAE / Edge / Path for the fade signals vs a random-bar baseline. NOT a backtest. Edge = horizon-return lift vs baseline. Path = net-excursion lift. n counts BARS (raw) or EPISODES (reversion). Cost-blind. Out-of-calibration only.")
os_color_mode    = input.string("State", "Overshoot column color", options=["State", "Direction"], group="Display", tooltip="State = up-extreme red (short), down-extreme green (long), grey otherwise. Direction = teal up / orange down by OS sign.")

// --- Scout sampling ---
fwd_bars      = input.int(24,  "Forward bars",            minval=1,                group="Scout", tooltip="Window over which MFE / MAE / horizon return are measured after each signal.")
min_samples   = input.int(30,  "Min samples to display",  minval=1,                group="Scout", tooltip="Fewer than this many post-calibration samples -> the cell shows n/a.")
lookback_bars = input.int(500, "Lookback window (bars)",  minval=50, maxval=10000, group="Scout", tooltip="Rolling sample window. Signal and baseline both reflect the SAME recent window. On 1h: 500 ~ 3 weeks.")
cal_len       = input.int(250, "Calibration burn-in (bars)", minval=30,            group="Scout", tooltip="Scout begins sampling only after this many bars (warm-up for the percentile rank).")

// --- Overshoot: extremity definition ---
pct_len     = input.int(250,   "Overshoot percentile lookback",       minval=30,                       group="Overshoot", tooltip="History for ranking overshoot extremity. ~250 bars is about 10 days on 1h.")
pct_thresh  = input.float(90,  "Overshoot percentile threshold (%)",  minval=50, maxval=99.9, step=0.5, group="Overshoot", tooltip="Extreme above this rank (or below 100 minus this), AND must clear the ATR floor. Both halves required.")
pctl_basis  = input.string("Pooled", "Percentile basis", options=["Pooled", "By-direction"], group="Overshoot", tooltip="Pooled (default) = up and down triggers are the high/low tail of ONE distribution of all overshoot bars, both signs together. By-direction = the up trigger ranks ONLY against past UP-overshoots and the down trigger ONLY against past DOWN-overshoots, so each side fires on what's extreme FOR ITS OWN SIGN (the asymmetry fix for biased MA anchors). The ATR floor still backstops both sides. Note: under By-direction the threshold % means 'top X% of THAT side', so per-side fire rates shift - recalibrate by watching scout n.")
os_min_atr  = input.float(1.0, "Min overshoot (ATR)",                 minval=0.0,             step=0.1, group="Overshoot", tooltip="Absolute floor (ATR) so a high percentile in a quiet window doesn't fire on a tiny move.")
k_decontam  = input.int(3,     "Baseline exclude-recent (bars)",      minval=0,                        group="Overshoot", tooltip="Fit the regression EXCLUDING the most recent k bars and project it forward, so the overshooting bar doesn't pull its own reference. Also lags the ATR normalizer by k AND excludes the recent k bars from the percentile ranking sample, so a dislocation can't lift its own trigger. 0 = original behavior. Typical: 2-5.")

// --- Anchors ---
os_anchor = input.string("Regression", "Overshoot anchor", options=["Regression", "SMA", "EMA"], group="Anchors", tooltip="What the overshoot measures price against. Regression (recommended) = least-squares trend line; keeps os centered near zero in clean trends. SMA / EMA lag, so os reads persistently offset in trends (the percentile gate partly compensates). All anchors are decontaminated (exclude the recent k bars) and projected forward k bars by their own slope.")
reg_len   = input.int(50,    "Anchor length",          minval=5, group="Anchors", tooltip="Window for the overshoot anchor (regression / SMA / EMA).")
carry_basis = input.string("VWAP", "Carry baseline",     options=["VWAP", "SMA", "EMA"], group="Anchors", tooltip="What the carry line measures the trend (reg) against: VWAP (volume-weighted value), SMA, or EMA of close. tc = (reg - baseline) / ATR.")
carry_len   = input.int(50,     "Carry baseline length", minval=5, group="Anchors", tooltip="Window for the carry baseline (VWAP / SMA / EMA).")
atr_len  = input.int(20, "ATR length (normalizer)", minval=2, group="Anchors", tooltip="Robust scale. All measures ATR-normalized.")

// ===== Helper: arm/fire latch =====
// Arms on extreme=true; fires ONCE when armed AND revert AND not extreme. max_bars = stale-arm timeout.
// gate_valid is kept for signature symmetry (no regime here, so callers pass true).
f_arm_fire(extreme, revert, gate_valid, max_bars) =>
    var bool armed   = false
    var int  arm_bar = 0
    fire = false
    if armed and not gate_valid
        armed := false
    if armed and (bar_index - arm_bar) > max_bars
        armed := false
    if extreme
        armed   := true
        arm_bar := bar_index
    if armed and revert and not extreme
        fire := true
        armed := false
    fire

// ===== Helper: k-decontaminate a baseline series =====
// Take the baseline as of k bars ago (its window ends k bars back), then project it forward k bars by
// its own bar-to-bar slope, so the recent k dislocating bars don't pull the anchor toward themselves.
f_decon(b, k) =>
    e = b[k]
    p = b[k + 1]
    e + (e - p) * k

// ===== Helper: linear-interpolation percentile of an arbitrary array =====
// Same (n-1) basis as ta.percentile_linear_interpolation, so Pooled and By-direction triggers
// are computed identically and stay comparable. Returns na on an empty array.
f_pctile(arr, p) =>
    n = array.size(arr)
    float res = na
    if n >= 1
        s = array.copy(arr)
        array.sort(s, order.ascending)
        rank = (p / 100.0) * (n - 1)
        lo   = int(math.floor(rank))
        hi   = int(math.ceil(rank))
        frac = rank - lo
        res := array.get(s, lo) + frac * (array.get(s, hi) - array.get(s, lo))
    res

// ===== Helpers: scout (array-based, rolling time window) =====
f_push_signal(bar_arr, mfe_arr, mae_arr, ret_arr, mfe_v, mae_v, ret_v) =>
    array.push(bar_arr, bar_index)
    array.push(mfe_arr, mfe_v)
    array.push(mae_arr, mae_v)
    array.push(ret_arr, ret_v)
    while array.size(bar_arr) > 0 and bar_index - array.first(bar_arr) > lookback_bars
        array.shift(bar_arr)
        array.shift(mfe_arr)
        array.shift(mae_arr)
        array.shift(ret_arr)

f_push_base(bar_arr, lm_a, la_a, lr_a, sm_a, sa_a, sr_a, lm_v, la_v, lr_v, sm_v, sa_v, sr_v) =>
    array.push(bar_arr, bar_index)
    array.push(lm_a, lm_v)
    array.push(la_a, la_v)
    array.push(lr_a, lr_v)
    array.push(sm_a, sm_v)
    array.push(sa_a, sa_v)
    array.push(sr_a, sr_v)
    while array.size(bar_arr) > 0 and bar_index - array.first(bar_arr) > lookback_bars
        array.shift(bar_arr)
        array.shift(lm_a)
        array.shift(la_a)
        array.shift(lr_a)
        array.shift(sm_a)
        array.shift(sa_a)
        array.shift(sr_a)

f_scout_mean(arr) =>
    n = array.size(arr)
    n < min_samples ? "n/a" : str.tostring(array.sum(arr) / n, "#.0")

f_edge_str(sig_ret, base_ret) =>
    sn = array.size(sig_ret)
    bn = array.size(base_ret)
    e  = sn < min_samples or bn < 1 ? na : array.sum(sig_ret) / sn - array.sum(base_ret) / bn
    na(e) ? "n/a" : (e > 0 ? "+" : "") + str.tostring(e, "#.0")
f_edge_col(sig_ret, base_ret) =>
    sn = array.size(sig_ret)
    bn = array.size(base_ret)
    e  = sn < min_samples or bn < 1 ? na : array.sum(sig_ret) / sn - array.sum(base_ret) / bn
    na(e) ? color.new(color.gray, 50) : e > 0.10 ? color.new(#00C853, 0) : e < -0.10 ? color.new(#FF1744, 0) : color.new(color.gray, 30)

f_net_edge_str(s_fav, s_adv, b_fav, b_adv) =>
    sn = array.size(s_fav)
    bn = array.size(b_fav)
    e  = sn < min_samples or bn < 1 ? na : (array.sum(s_fav) - array.sum(s_adv)) / sn - (array.sum(b_fav) - array.sum(b_adv)) / bn
    na(e) ? "n/a" : (e > 0 ? "+" : "") + str.tostring(e, "#.0")
f_net_edge_col(s_fav, s_adv, b_fav, b_adv) =>
    sn = array.size(s_fav)
    bn = array.size(b_fav)
    e  = sn < min_samples or bn < 1 ? na : (array.sum(s_fav) - array.sum(s_adv)) / sn - (array.sum(b_fav) - array.sum(b_adv)) / bn
    na(e) ? color.new(color.gray, 50) : e > 0.10 ? color.new(#00C853, 0) : e < -0.10 ? color.new(#FF1744, 0) : color.new(color.gray, 30)

// ===== Components =====
atr      = ta.atr(atr_len)
safe_atr = atr > 0 ? atr : na

// Overshoot anchor (selectable: regression / SMA / EMA), all decontaminated + forward-projected.
// Regression uses its true least-squares slope (offset method); MAs use f_decon (endpoint velocity).
reg_lr_end  = ta.linreg(close[k_decontam], reg_len, 0)
reg_lr_prev = ta.linreg(close[k_decontam], reg_len, 1)
reg_lr      = reg_lr_end + (reg_lr_end - reg_lr_prev) * k_decontam
reg_sma     = f_decon(ta.sma(close, reg_len), k_decontam)
reg_ema     = f_decon(ta.ema(close, reg_len), k_decontam)
reg         = os_anchor == "SMA" ? reg_sma : os_anchor == "EMA" ? reg_ema : reg_lr

// Lagged ATR normalizer.
atr_os = safe_atr[k_decontam]

// Carry baseline: VWAP / SMA / EMA of close (selectable). Compute all unconditionally, then select
// (avoids conditional ta.* calls).
sma_v      = ta.sma(close, carry_len)
ema_v      = ta.ema(close, carry_len)
vol_sum    = math.sum(volume, carry_len)
pv_sum     = math.sum(close * volume, carry_len)
rvwap      = vol_sum > 0 ? pv_sum / vol_sum : na
carry_base = carry_basis == "SMA" ? sma_v : carry_basis == "EMA" ? ema_v : rvwap

// Overshoot (the oscillator) and Carry (displacement vs the chosen baseline).
os = (na(atr_os) or na(reg))        ? na : (close - reg)      / atr_os
tc = (na(atr_os) or na(carry_base)) ? na : (reg - carry_base) / atr_os

// ===== Extremity = dynamic trigger band (the OUTER of the ATR floor and the percentile level) =====
// os >= max(os_min_atr, 90th-pctile-os) is EXACTLY (os >= floor AND os_pct >= pct_thresh), just expressed
// as a single LEVEL we can plot - so the line on screen IS the fire threshold. Mirror for the down side.
// Basis:
//   Pooled       = high/low tail of ONE distribution of all os (built-in percentile, both signs pooled).
//   By-direction = up tail ranks only past UP-overshoots, down tail only past DOWN-overshoots, so each side
//                  fires on what's extreme for ITS OWN sign. ATR floor still backstops; tiny subsets fall to floor.
os_pct  = ta.percentrank(os, pct_len)                                   // kept for the panel readout

// Pooled percentiles (ta.* must run unconditionally on every bar). Read [k_decontam] bars back so the
// ranking window ENDS k bars ago - the recent dislocating bars don't sit in their own threshold sample.
os_p_hi_pool = ta.percentile_linear_interpolation(os, pct_len, pct_thresh)[k_decontam]
os_p_lo_pool = ta.percentile_linear_interpolation(os, pct_len, 100 - pct_thresh)[k_decontam]

// Rolling window of recent os, for the sign-conditioned (By-direction) percentiles.
var array<int>   os_w_bar = array.new<int>()
var array<float> os_w_val = array.new<float>()
if not na(os)
    array.push(os_w_bar, bar_index)
    array.push(os_w_val, os)
    while array.size(os_w_bar) > 0 and bar_index - array.first(os_w_bar) >= pct_len + k_decontam
        array.shift(os_w_bar)
        array.shift(os_w_val)

os_pos = array.new<float>()
os_neg = array.new<float>()
wn = array.size(os_w_val)
if pctl_basis == "By-direction" and wn > 0
    for i = 0 to wn - 1
        // Exclude the most recent k bars (match the pooled [k_decontam] lag).
        if bar_index - array.get(os_w_bar, i) >= k_decontam
            v = array.get(os_w_val, i)
            if v > 0
                array.push(os_pos, v)
            if v < 0
                array.push(os_neg, v)
// Need a minimum same-sign sample before trusting a sign-conditioned tail; else fall back to the floor.
os_p_hi_dir = array.size(os_pos) >= 10 ? f_pctile(os_pos, pct_thresh)       : na
os_p_lo_dir = array.size(os_neg) >= 10 ? f_pctile(os_neg, 100 - pct_thresh) : na

os_p_hi = pctl_basis == "By-direction" ? os_p_hi_dir : os_p_hi_pool
os_p_lo = pctl_basis == "By-direction" ? os_p_lo_dir : os_p_lo_pool

trig_up = math.max(os_min_atr,  nz(os_p_hi,  os_min_atr))
trig_dn = math.min(-os_min_atr, nz(os_p_lo, -os_min_atr))
os_ext_up  = not na(os) and os >= trig_up   // stretched UP  -> fade SHORT
os_ext_dn  = not na(os) and os <= trig_dn   // stretched DOWN -> fade LONG
os_extreme = os_ext_up or os_ext_dn

// Numeric trace codes for the Data Window (1 = percentile gate bound this bar, 0 = ATR floor bound).
bind_up_code = nz(os_p_hi, -1e9) >= os_min_atr  ? 1 : 0
bind_dn_code = nz(os_p_lo,  1e9) <= -os_min_atr ? 1 : 0

// ===== Fade signals =====
// up-extreme = SHORT (fade the stretch down); down-extreme = LONG (fade the stretch up).
fade_up_raw = os_ext_up   // SHORT
fade_dn_raw = os_ext_dn   // LONG

// Reversion latch: fade-short reverts when os pulls back DOWN (os < os[1]); fade-long when os pulls UP.
fade_up_fire = f_arm_fire(fade_up_raw, not na(os) and not na(os[1]) and os < os[1], true, arm_max_bars)
fade_dn_fire = f_arm_fire(fade_dn_raw, not na(os) and not na(os[1]) and os > os[1], true, arm_max_bars)

fade_up_sig = use_reversion ? fade_up_fire : fade_up_raw
fade_dn_sig = use_reversion ? fade_dn_fire : fade_dn_raw

// ===== Scout accumulators (rolling time window of MFE / MAE / return in ATR units) =====
var array<int>   fl_bar  = array.new<int>()
var array<int>   fs_bar  = array.new<int>()
var array<int>   bs_bar  = array.new<int>()
var array<float> fl_mfe  = array.new<float>()
var array<float> fl_mae  = array.new<float>()
var array<float> fl_ret  = array.new<float>()
var array<float> fs_mfe  = array.new<float>()
var array<float> fs_mae  = array.new<float>()
var array<float> fs_ret  = array.new<float>()
var array<float> bl_mfe  = array.new<float>()
var array<float> bl_mae  = array.new<float>()
var array<float> bl_ret  = array.new<float>()
var array<float> bsh_mfe = array.new<float>()
var array<float> bsh_mae = array.new<float>()
var array<float> bsh_ret = array.new<float>()

hh_w = ta.highest(high, fwd_bars)
ll_w = ta.lowest(low,  fwd_bars)

post_cal = bar_index - fwd_bars >= cal_len
if post_cal
    entry   = close[fwd_bars]
    atr_sig = safe_atr[fwd_bars]
    if not na(atr_sig) and atr_sig > 0 and not na(entry)
        up_ext   = (hh_w  - entry) / atr_sig
        down_ext = (entry - ll_w)  / atr_sig
        long_mfe  = up_ext
        long_mae  = down_ext
        long_ret  = (close - entry) / atr_sig
        short_mfe = up_ext
        short_mae = down_ext
        short_ret = (entry - close) / atr_sig

        f_push_base(bs_bar, bl_mfe, bl_mae, bl_ret, bsh_mfe, bsh_mae, bsh_ret,
                    long_mfe, long_mae, long_ret, short_mfe, short_mae, short_ret)

        if fade_dn_sig[fwd_bars]   // down-extreme = LONG
            f_push_signal(fl_bar, fl_mfe, fl_mae, fl_ret, long_mfe, long_mae, long_ret)
        if fade_up_sig[fwd_bars]   // up-extreme = SHORT
            f_push_signal(fs_bar, fs_mfe, fs_mae, fs_ret, short_mfe, short_mae, short_ret)

// ===== Arrow firing =====
committed = barstate.isconfirmed
fade_short = fade_up_sig   // red, above bar
fade_long  = fade_dn_sig   // green, below bar

// ===== Histogram color =====
// Colored only when EXTREME (both gates cleared): up-extreme red (short), down-extreme green (long).
color st_col     = os_ext_up ? color.new(#FF1744, 0) : os_ext_dn ? color.new(#00C853, 0) : color.new(color.gray, 40)
color os_dir_col = os > 0 ? color.new(#26C6DA, 30) : os < 0 ? color.new(#FFA726, 30) : color.new(color.gray, 50)
color os_col     = os_color_mode == "Direction" ? os_dir_col : color.new(st_col, 30)
color tc_col     = color.new(#BB86FC, 0)

// ===== Plots (oscillator pane) =====
plot(os,     "Overshoot (ATR)",            style=plot.style_columns, color=os_col)
plot(tc,     "Carry / displacement (ATR)", color=tc_col, linewidth=2)
plot(os_pct, "Overshoot percentile",       color=color.new(color.gray, 100), display=display.data_window)

// --- Data Window trace (hover any bar to read the full trigger derivation, bar by bar) ---
plot(os_p_hi,      "Pctile lvl + (pre-floor)", color=color.new(color.gray, 100), display=display.data_window)
plot(os_p_lo,      "Pctile lvl - (pre-floor)", color=color.new(color.gray, 100), display=display.data_window)
plot(os_min_atr,   "ATR floor (+/-)",          color=color.new(color.gray, 100), display=display.data_window)
plot(bind_up_code, "Binding + (1=pctile,0=floor)", color=color.new(color.gray, 100), display=display.data_window)
plot(bind_dn_code, "Binding - (1=pctile,0=floor)", color=color.new(color.gray, 100), display=display.data_window)

// Dynamic trigger band = the live fire level (the outer of the ATR floor and the percentile level).
// os poking past this band IS a fade signal - the line on screen equals the trigger.
pTrigUp = plot(trig_up, "Trigger + (fire level)", color=color.new(color.gray, 45), style=plot.style_stepline)
pTrigDn = plot(trig_dn, "Trigger - (fire level)", color=color.new(color.gray, 45), style=plot.style_stepline)
fill(pTrigUp, pTrigDn, color.new(color.gray, 92), "No-fire band")
hline(0, "Zero", color=color.new(color.gray, 0), linestyle=hline.style_solid)

// ===== Arrows on the PRICE pane (committed solid + provisional faint) =====
plotshape(show_turns and committed and fade_short ? high : na, "FADE SHORT", shape.triangledown, location.abovebar, color=color.new(#FF1744, 0), size=size.tiny, force_overlay=true)
plotshape(show_turns and committed and fade_long  ? low  : na, "FADE LONG",  shape.triangleup,   location.belowbar, color=color.new(#00C853, 0), size=size.tiny, force_overlay=true)
plotshape(show_turns and show_provisional and not committed and fade_short ? high : na, "FADE SHORT (prov)", shape.triangledown, location.abovebar, color=color.new(color.gray, 60), size=size.tiny, force_overlay=true)
plotshape(show_turns and show_provisional and not committed and fade_long  ? low  : na, "FADE LONG (prov)",  shape.triangleup,   location.belowbar, color=color.new(color.gray, 60), size=size.tiny, force_overlay=true)

// ===== State panel =====
string ext_label = os_ext_up ? "Up-extreme (SHORT)" : os_ext_dn ? "Down-extreme (LONG)" : "-"
color  ext_bg    = os_ext_up ? color.new(#FF1744, 0) : os_ext_dn ? color.new(#00C853, 0) : color.new(color.gray, 40)

var table panel = table.new(position.top_right, 2, 6, border_color=color.new(color.gray, 50), border_width=1, frame_color=color.new(color.gray, 50), frame_width=1)
if show_table and barstate.islast
    string forming = barstate.isconfirmed ? " (confirmed)" : " (forming)"
    table.cell(panel, 0, 0, "Overshoot",     text_color=color.silver, text_size=size.tiny)
    table.cell(panel, 1, 0, str.tostring(os, "#.00") + " ATR (" + (os > 0 ? "Up" : os < 0 ? "Down" : "flat") + ")", text_color=color.white, text_size=size.tiny)
    table.cell(panel, 0, 1, "OS Percentile", text_color=color.silver, text_size=size.tiny)
    table.cell(panel, 1, 1, str.tostring(os_pct, "#.0") + " %", text_color=color.white, text_size=size.tiny)
    table.cell(panel, 0, 2, "Extremity",     text_color=color.silver, text_size=size.tiny)
    table.cell(panel, 1, 2, ext_label,       text_color=color.white, bgcolor=ext_bg, text_size=size.tiny)
    table.cell(panel, 0, 3, "Carry",         text_color=color.silver, text_size=size.tiny)
    table.cell(panel, 1, 3, str.tostring(tc, "#.00") + " ATR (" + carry_basis + ")", text_color=color.white, text_size=size.tiny)
    table.cell(panel, 0, 4, "Mode",          text_color=color.silver, text_size=size.tiny)
    table.cell(panel, 1, 4, (use_reversion ? "Reversion" : "Raw") + " / " + os_anchor + " / " + (pctl_basis == "By-direction" ? "dir" : "pool") + forming, text_color=color.white, text_size=size.tiny)
    table.cell(panel, 0, 5, "Trigger",       text_color=color.silver, text_size=size.tiny)
    table.cell(panel, 1, 5, str.tostring(trig_up, "#.00") + " / " + str.tostring(trig_dn, "#.00") + " ATR", text_color=color.white, text_size=size.tiny)

// ===== Scout dashboard =====
var table scout_panel = table.new(position.bottom_right, 6, 3, border_color=color.new(color.gray, 50), border_width=1, frame_color=color.new(color.gray, 50), frame_width=1)
if show_scout and barstate.islast
    table.cell(scout_panel, 0, 0, "",     text_color=color.silver, bgcolor=color.new(color.black, 50), text_size=size.tiny)
    table.cell(scout_panel, 1, 0, "MFE",  text_color=color.silver, bgcolor=color.new(color.black, 50), text_size=size.tiny)
    table.cell(scout_panel, 2, 0, "MAE",  text_color=color.silver, bgcolor=color.new(color.black, 50), text_size=size.tiny)
    table.cell(scout_panel, 3, 0, "Edge", text_color=color.silver, bgcolor=color.new(color.black, 50), text_size=size.tiny)
    table.cell(scout_panel, 4, 0, "Path", text_color=color.silver, bgcolor=color.new(color.black, 50), text_size=size.tiny)
    table.cell(scout_panel, 5, 0, "n",    text_color=color.silver, bgcolor=color.new(color.black, 50), text_size=size.tiny)
    // Fade Long (down-extreme)
    table.cell(scout_panel, 0, 1, "Fade Long",                                          text_color=color.silver, bgcolor=color.new(color.black, 50), text_size=size.tiny)
    table.cell(scout_panel, 1, 1, f_scout_mean(fl_mfe),                                 text_color=color.white, text_size=size.tiny)
    table.cell(scout_panel, 2, 1, f_scout_mean(fl_mae),                                 text_color=color.white, text_size=size.tiny)
    table.cell(scout_panel, 3, 1, f_edge_str(fl_ret, bl_ret),                           text_color=color.white, bgcolor=f_edge_col(fl_ret, bl_ret),                           text_size=size.tiny)
    table.cell(scout_panel, 4, 1, f_net_edge_str(fl_mfe, fl_mae, bl_mfe, bl_mae),       text_color=color.white, bgcolor=f_net_edge_col(fl_mfe, fl_mae, bl_mfe, bl_mae),       text_size=size.tiny)
    table.cell(scout_panel, 5, 1, str.tostring(array.size(fl_mfe)),                     text_color=color.white, text_size=size.tiny)
    // Fade Short (up-extreme)
    table.cell(scout_panel, 0, 2, "Fade Short",                                         text_color=color.silver, bgcolor=color.new(color.black, 50), text_size=size.tiny)
    table.cell(scout_panel, 1, 2, f_scout_mean(fs_mfe),                                 text_color=color.white, text_size=size.tiny)
    table.cell(scout_panel, 2, 2, f_scout_mean(fs_mae),                                 text_color=color.white, text_size=size.tiny)
    table.cell(scout_panel, 3, 2, f_edge_str(fs_ret, bsh_ret),                          text_color=color.white, bgcolor=f_edge_col(fs_ret, bsh_ret),                         text_size=size.tiny)
    table.cell(scout_panel, 4, 2, f_net_edge_str(fs_mae, fs_mfe, bsh_mae, bsh_mfe),     text_color=color.white, bgcolor=f_net_edge_col(fs_mae, fs_mfe, bsh_mae, bsh_mfe),     text_size=size.tiny)
    table.cell(scout_panel, 5, 2, str.tostring(array.size(fs_mfe)),                     text_color=color.white, text_size=size.tiny)

// ===== Alerts (committed-only) =====
alertcondition(fade_up_sig and committed, "FADE SHORT", "Overshoot extreme up - fade short")
alertcondition(fade_dn_sig and committed, "FADE LONG",  "Overshoot extreme down - fade long")
```
