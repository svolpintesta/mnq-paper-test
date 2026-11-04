"""Explains, step by step, why the engine did or did not trade on one day.
Usage:  python mnq_verify_day.py mnq_eod_live/bars/bars_2026-11-02.csv mnq_eod_live/history_days.json 2026-11-02
(history file may include the day itself; it is cut off before that date.)"""
import sys, json, pandas as pd
import mnq_live_engine as E
bars = pd.read_csv(sys.argv[1]); hist = [x for x in json.load(open(sys.argv[2])) if x['date'] < sys.argv[3]]
eng = E.Engine(hist, dict(strategies=['Gap', 'ORB', 'NR7'])); eng.new_day(sys.argv[3])
print(f'history sessions {len(hist)} | ready={eng.ready} {eng.reason}')
print(f'prior close {eng.pdc:.2f} | dr {eng.dr:.1f} pts | trend {eng.trend:+d} | NR7 day: {eng.nr7}')
o = bars.open.values; h = bars.high.values; l = bars.low.values; c = bars.close.values
gap = o[0] - eng.pdc
print(f'\nGAP AND GO: open {o[0]:.2f}, gap {gap:+.1f} pts, needs |gap| >= {0.2*eng.dr:.1f} -> {"big enough" if abs(gap) >= 0.2*eng.dr else "too small, no trade"}')
if abs(gap) >= 0.2*eng.dr: print(f'  bar 4 close {c[4]:.2f} vs open: move {c[4]-o[0]:+.1f}; trade needs the move in the gap direction -> {"CONFIRMED" if (c[4]-o[0])*(1 if gap>0 else -1) >= 0 else "NOT confirmed, no trade"}')
orh, orl = h[:15].max(), l[:15].min(); print(f'\nORB: opening range (first 15 min) high {orh:.2f} low {orl:.2f}; trend filter requires direction {eng.trend:+d}')
import numpy as np
tp = (h + l + c) / 3; vw = np.cumsum(tp * np.maximum(bars.volume.values, 1)) / np.cumsum(np.maximum(bars.volume.values, 1))
first = None
for k in range(2, 30):
    i = (k + 1) * 5 - 1; ci = c[i]
    d = 1 if (ci > orh and ci > vw[i]) else (-1 if (ci < orl and ci < vw[i]) else 0)
    if d: first = (i, d, ci); break
if first: print(f'  first 5-min close outside the range (and on the right side of VWAP): bar {first[0]} ({9+(30+first[0])//60}:{(30+first[0])%60:02d}), direction {first[1]:+d}, close {first[2]:.2f} -> {"TRADE" if first[1]==eng.trend else "wrong direction vs trend filter, no trade"}')
else: print('  no 5-min close outside the opening range with VWAP confirmation before 12:00 -> no trade')
print(f'\nNR7: today is an NR7 day: {eng.nr7} -> ' + ('no trade' if not eng.nr7 else 'same breakout logic as ORB without trend filter'))
for i in range(389): ev = eng.on_bar(i, o[i], h[i], l[i], c[i], bars.volume.values[i])
print(f'\nENGINE RESULT: {len(eng.trades)} trade(s): ' + '; '.join(f'{t["strat"]} {"LONG" if t["d"]==1 else "SHORT"} bar {t["i0"]}->{t["ex"]} ${t["pnl"]:.2f}' for t in eng.trades))
print(f'day range {h.max()-l.min():.0f} pts, open {o[0]:.2f}, close {c[388]:.2f}')
