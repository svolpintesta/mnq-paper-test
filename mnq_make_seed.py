"""Build history_days.json (the last ~25 completed sessions) from any 1-minute bar CSV, so the daily runner can start trading
right away instead of waiting 20 days.   python mnq_make_seed.py bars.csv  my_workdir  [--last 25]
Accepted CSVs: (a) the NQ dataset layout  'MM/DD/YYYY HH:MM,open,high,low,close,volume,...'  (times in New York time)
               (b) the bot's forward_raw_bars_v1.csv layout (columns ts_et, open, high, low, close, volume)
Use one continuous contract (no roll gap inside the 25 sessions) and New York time stamps."""
import sys, json
import pandas as pd
from pathlib import Path

def load(path):
    head = open(path).readline().lower()
    if 'ts_et' in head:
        df = pd.read_csv(path)
        ts = pd.to_datetime(df['ts_et'].str.slice(0, 19))
        df = pd.DataFrame(dict(ts=ts, o=df.open, h=df.high, l=df.low, c=df.close))
    else:
        df = pd.read_csv(path)
        df = df.iloc[:, :5]; df.columns = ['ts', 'o', 'h', 'l', 'c']
        df['ts'] = pd.to_datetime(df.ts, format='%m/%d/%Y %H:%M')
    return df.drop_duplicates('ts').sort_values('ts')

def build(df, last=25):
    m = df.ts.dt.hour * 60 + df.ts.dt.minute - 570
    df = df[(m >= 0) & (m < 389) & (df.ts.dt.weekday < 5)].assign(idx=m, d=df.ts.dt.normalize())
    out = []
    for d, g in df.groupby('d'):
        if len(g) < 380 or g.idx.max() < 385:
            continue
        out.append(dict(date=str(d.date()), h=float(g.h.max()), l=float(g.l.min()), c=float(g.sort_values('idx').c.iloc[-1])))
    return out[-last:]

if __name__ == '__main__':
    src, wd = sys.argv[1], Path(sys.argv[2]); wd.mkdir(parents=True, exist_ok=True)
    last = int(sys.argv[sys.argv.index('--last') + 1]) if '--last' in sys.argv else 25
    h = build(load(src), last)
    json.dump(h, open(wd / 'history_days.json', 'w'), indent=1)
    print(f'wrote {len(h)} sessions to {wd / "history_days.json"}  (first {h[0]["date"]}, last {h[-1]["date"]})' if h else 'no complete sessions found')
    if len(h) < 20: print('WARNING: fewer than 20 sessions; the engine will wait until it has 20.')
