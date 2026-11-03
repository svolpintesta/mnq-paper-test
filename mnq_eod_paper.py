"""End-of-day paper test: no broker, no Quantower, no live feed.
Once per day, after the close, it downloads the finished 1-minute bars, replays every new session through the SAME engine
(mnq_live_engine.py, proven identical to the backtest), and appends to a ledger + daily report.
If the computer was off for a few days it catches up in order, so nothing is skipped.

Sources:  --source databento   (needs: pip install databento, and env var DATABENTO_API_KEY; free credits for new accounts)
          --source yahoo       (needs: pip install yfinance; no account; only ~7 days of 1-minute history, unofficial)
          --source csv --file bars.csv   (any 1-minute CSV in the NQ-dataset or Quantower-export layout; used for testing)
First run with databento also pulls ~45 days of warm-up so it can start trading signals on day 21 (the warm-up days are paper-traded too).
"""
import argparse, csv, json, os, sys, urllib.request, urllib.parse
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import mnq_live_engine as E

ET = ZoneInfo('America/New_York')


# ---------------- data sources -> canonical frame: ts (New York, naive, bar OPEN time), o,h,l,c,v,sym ----------------
def src_csv(path, **_):
    head = open(path).readline().lower()
    df = pd.read_csv(path)
    if 'ts_et' in head:
        out = pd.DataFrame(dict(ts=pd.to_datetime(df['ts_et'].str.slice(0, 19)), o=df.open, h=df.high, l=df.low, c=df.close, v=df.volume))
        out['sym'] = df['symbol'] if 'symbol' in df else None
    else:
        df = df.iloc[:, :6]; df.columns = ['ts', 'o', 'h', 'l', 'c', 'v']
        df['ts'] = pd.to_datetime(df.ts, format='%m/%d/%Y %H:%M'); out = df.assign(sym=None)
    return out


def src_databento(start, end, max_cost, symbol='MNQ.v.0', **_):
    import databento as db
    key = os.environ.get('DATABENTO_API_KEY')
    if not key: sys.exit('Set the environment variable DATABENTO_API_KEY first.')
    c = db.Historical(key)
    kw = dict(dataset='GLBX.MDP3', symbols=[symbol], stype_in='continuous', schema='ohlcv-1m',
              start=f'{start}T00:00:00', end=f'{end + timedelta(days=1)}T00:00:00')
    cost = c.metadata.get_cost(**kw)
    print(f'databento quoted cost for {start}..{end}: ${cost:.4f}')
    if cost > max_cost: sys.exit(f'Refusing: cost ${cost:.2f} is above --max-cost ${max_cost:.2f}')
    df = c.timeseries.get_range(**kw).to_df().reset_index()
    ts = pd.to_datetime(df['ts_event'], utc=True).dt.tz_convert(ET).dt.tz_localize(None)
    return pd.DataFrame(dict(ts=ts, o=df.open, h=df.high, l=df.low, c=df.close, v=df.volume, sym=df.get('symbol')))


def src_yahoo(**_):
    import yfinance as yf
    df = yf.download('NQ=F', interval='1m', period='8d', auto_adjust=False, progress=False, prepost=False)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    idx = df.index.tz_convert(ET).tz_localize(None) if df.index.tz is not None else df.index
    return pd.DataFrame(dict(ts=idx, o=df.Open.values, h=df.High.values, l=df.Low.values, c=df.Close.values, v=df.Volume.values, sym=None))


def third_friday(y, m):
    d = date(y, m, 15)
    while d.weekday() != 4: d += timedelta(days=1)
    return d


def roll_skip_dates(d0, d1):
    """Yahoo gives no contract name, so skip Thu, Fri and the following Mon around each quarterly expiry (crude)."""
    out = []
    for y in range(d0.year, d1.year + 1):
        for m in (3, 6, 9, 12):
            f = third_friday(y, m); out += [str(f - timedelta(days=1)), str(f), str(f + timedelta(days=3))]
    return out


# ---------------- session assembly ----------------
def sessions(df):
    """yield (date, bars[389] as list of (o,h,l,c,v), symbol, n_real, n_missing) for every date with usable data"""
    m = df.ts.dt.hour * 60 + df.ts.dt.minute - 570
    df = df[(m >= 0) & (m < 389) & (df.ts.dt.weekday < 5)].assign(idx=m, d=df.ts.dt.normalize()).drop_duplicates(['ts'])
    for d, g in df.groupby('d'):
        g = g.sort_values('idx')
        yield d.date(), g


def build_day(g):
    """returns (bars, missing) or (None, reason)"""
    if g.idx.min() > 2 or g.idx.max() < 385: return None, f'incomplete session (bars {g.idx.min()}..{g.idx.max()}, {len(g)} rows): half day or missing data'
    by = {int(r.idx): r for r in g.itertuples()}
    bars = []; missing = 0; prev = None
    for i in range(389):
        r = by.get(i)
        if r is None:
            if prev is None: return None, 'first bars missing'
            bars.append((prev, prev, prev, prev, 0.0)); missing += 1
        else:
            bars.append((float(r.o), float(r.h), float(r.l), float(r.c), float(r.v))); prev = float(r.c)
    if missing > 5: return None, f'{missing} bars missing'
    return (bars, missing), None


def notify(text):
    tok, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not (tok and chat): return
    try:
        urllib.request.urlopen(urllib.request.Request(f'https://api.telegram.org/bot{tok}/sendMessage',
                               urllib.parse.urlencode(dict(chat_id=chat, text=text)).encode()), timeout=10)
    except Exception as e:
        print('telegram failed:', e)


def summary(ledger):
    if not ledger.exists(): return 'no trades yet'
    rows = list(csv.DictReader(open(ledger))); p = [float(r['paper_pnl_usd']) for r in rows]
    if not p: return 'no trades yet'
    w = sum(x for x in p if x > 0); l = -sum(x for x in p if x < 0); eq = 0; pk = 0; dd = 0
    for x in p: eq += x; pk = max(pk, eq); dd = min(dd, eq - pk)
    return (f'{len(p)} trades, net ${sum(p):.0f}, win {100 * sum(x > 0 for x in p) / len(p):.0f}%, PF {w / l if l else float("inf"):.2f}, '
            f'avg ${sum(p) / len(p):.1f}/trade, worst drawdown ${dd:.0f}   (backtest: avg $28/trade, win 44%, PF 1.38; judge only after 60+ trades)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', default='mnq_eod_live'); ap.add_argument('--source', choices=['databento', 'yahoo', 'csv'], required=True)
    ap.add_argument('--file'); ap.add_argument('--strategies', default='Gap,ORB,NR7'); ap.add_argument('--profile', default='PRACTICE')
    ap.add_argument('--warmup-days', type=int, default=45, help='calendar days to pull on the very first run (databento)')
    ap.add_argument('--max-cost', type=float, default=1.0, help='abort a databento download that would cost more than this many USD')
    ap.add_argument('--symbol', default='MNQ.v.0')
    a = ap.parse_args()
    wd = Path(a.workdir); wd.mkdir(parents=True, exist_ok=True)
    hist_p, st_p, led = wd / 'history_days.json', wd / 'eod_state.json', wd / 'ledger.csv'
    st = json.load(open(st_p)) if st_p.exists() else {}
    hist = E.load_history(hist_p)
    gate_p = wd / 'gate_state.json'
    gate = None if a.profile == 'NONE' else E.RiskGate(a.profile, json.load(open(gate_p)) if gate_p.exists() else None)
    now = datetime.now(ET)
    last = date.fromisoformat(st['last_date']) if st.get('last_date') else None
    today_done = now.hour * 60 + now.minute >= 16 * 60 + 20
    newest = now.date() if today_done else now.date() - timedelta(days=1)
    if a.source == 'csv':
        df = src_csv(a.file); newest = df.ts.max().date()
    elif a.source == 'databento':
        first = (last + timedelta(days=1)) if last else (newest - timedelta(days=a.warmup_days))
        if first > newest: print('nothing new to process'); return
        df = src_databento(first, newest, a.max_cost, a.symbol)
    else:
        df = src_yahoo()
    skip = roll_skip_dates(newest - timedelta(days=400), newest) if a.source == 'yahoo' else []
    eng = E.Engine(hist, dict(strategies=a.strategies.split(','), skip_dates=skip), gate)
    new_rows = 0; lines_out = []
    for d, g in sessions(df):
        if (last and d <= last) or d > newest: continue
        res, why = build_day(g)
        if res is None:
            msg = f'{d}: skipped, {why}'; print(msg); lines_out.append(msg); continue
        bars, missing = res
        sym = None
        if 'sym' in g and g.sym.notna().any(): sym = str(g.sym.dropna().mode().iloc[0])
        eng.new_day(str(d))
        if st.get('symbol') and sym and st['symbol'] != sym:
            eng.ready = False; eng.reason = f'contract changed {st["symbol"]} -> {sym}: no trading today'
        if missing >= 3: eng.halted = True
        for i, (o, h, l, c, v) in enumerate(bars): eng.on_bar(i, o, h, l, c, v)
        out = eng.finish_day()
        if sym: st['symbol'] = sym
        st['last_date'] = str(d)
        newled = not led.exists()
        with open(led, 'a', newline='') as f:
            w = csv.writer(f)
            if newled: w.writerow(['date', 'strategy', 'side', 'entry_bar', 'exit_bar', 'exit_reason', 'paper_pnl_usd'])
            for t in out['trades']: w.writerow([d, t['strat'], 'LONG' if t['d'] == 1 else 'SHORT', t['i0'], t['ex'], t['why'], round(t['pnl'], 2)])
        rep = [f'# EOD paper report {d}', f'- source {a.source}, symbol {sym}, missing bars {missing}',
               f'- engine ready: {out["ready"]} {out["reason"]}', f'- trades {len(out["trades"])}, paper P&L ${out["pnl"]:.2f}']
        rep += [f'  - {t["strat"]} {"LONG" if t["d"] == 1 else "SHORT"} bar {t["i0"]} -> {t["ex"]} ({t["why"]}) ${t["pnl"]:.2f}' for t in out['trades']]
        (wd / f'report_{d}.md').write_text('\n'.join(rep) + '\n')
        lines_out.append(f'{d}: ' + ('warm-up' if not out['ready'] and 'warming' in out['reason'] else f'{len(out["trades"])} trade(s), ${out["pnl"]:.2f}') +
                         (f' [{out["reason"]}]' if out['reason'] and 'warming' not in out['reason'] else ''))
        new_rows += 1
        E.save_history(hist_p, eng.hist); json.dump(st, open(st_p, 'w'))
        if gate: json.dump(gate.state(), open(gate_p, 'w'))
    msg = f'MNQ paper test: {new_rows} new session(s) processed\n' + '\n'.join(lines_out[-8:]) + '\n' + summary(led)
    print(msg); notify(msg)


if __name__ == '__main__':
    main()
