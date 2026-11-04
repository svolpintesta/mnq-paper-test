"""MNQ live signal engine (bar-by-bar, no broker code).

Feed it completed 1-minute RTH bars in order (idx 0 = 09:30 ET bar ... idx 385 = 15:55 bar).
It returns events. It never sees a future bar, so what it does in replay is what it does live.

Rules are identical to the backtest in mnq_round3/4/6_strategies.py and mnq_backtest_engine.py
(parity-tested by mnq_parity_test.py). Definitions copied from the backtest:
  daily range  = max(high) - min(low) over bars 0..388, daily close = close of bar 388
  dr           = mean daily range of the last 10 sessions
  trend        = +1 if last close > mean of last 20 closes else -1
  entries      = market order at the open of the bar after the signal bar
  exits        = resting stop (and target if any); anything still open is closed after bar 385 (15:55 bar)
"""
import json, copy

PV = 2.0
COMM_RT = 1.40
EXIT_IDX = 385
MIN_HISTORY = 20

DEFAULT = dict(
    slip=0.5,                       # points assumed per side in paper fills
    strategies=['Gap', 'ORB', 'NR7'],   # list order = priority when two signals share a bar
    one_at_a_time=True,             # the Quantower bridge refuses overlapping positions
    qty=1,
    skip_dates=[],                  # e.g. contract-roll days "2026-12-10"
)

PARAMS = dict(
    Gap=dict(g=0.2, K=0.25),
    VBgap=dict(k=0.5, K=0.4),
    ORB=dict(N=15, R=1.5, cap=0.3),
    NR7=dict(N=15, R=2.0, cap=0.3, minstop=3),
    Trend30=dict(m=0.3, K=0.4),
    ORBR=dict(R=1.5, cap=0.3, tol=0.04, window=12),   # ORB retest variant (shadow track)
)

# account-rule profiles for the pre-trade gate (same logic as mnq_prop_firm_simulator.py)
PROFILES = dict(
    LUCID25K_EOD=dict(B0=25000, MLL=1000, lockadd=100, DLL=600, selfstop=360, K=1.5, buf=100, nohedge=True),
    LUCID50K_EOD=dict(B0=50000, MLL=2000, lockadd=100, DLL=1200, selfstop=720, K=1.5, buf=100, nohedge=True),
    LUCID100K_EOD=dict(B0=100000, MLL=3000, lockadd=100, DLL=1800, selfstop=1080, K=1.5, buf=100, nohedge=True),
    PRACTICE=dict(B0=50000, MLL=10**6, lockadd=0, DLL=None, selfstop=500, K=0.0, buf=0, nohedge=True),
)


class RiskGate:
    """Account guard. EOD trailing drawdown with lock, daily limit cushion, daily self-stop."""

    def __init__(self, profile, state=None):
        self.p = dict(PROFILES[profile]) if isinstance(profile, str) else dict(profile)
        s = state or {}
        self.bal0 = s.get('bal0', self.p['B0'])      # balance at start of today (closed-day balance)
        self.peak = s.get('peak', self.p['B0'])      # highest end-of-day balance
        self.realized = 0.0
        self.halted = False

    def sync_balance(self, bal, have_state):
        """Start of day: take the broker's closed balance as the truth. peak = highest end-of-day balance we know about."""
        self.bal0 = float(bal)
        self.peak = max(self.peak, self.bal0) if have_state else max(self.p['B0'], self.bal0)

    def floor(self):
        p = self.p
        return min(p['B0'] + p.get('lockadd', 0), self.peak - p['MLL'])

    def allow(self, risk_usd, open_risk_usd):
        p = self.p
        if self.halted:
            return False, 'halted for the day'
        orisk = open_risk_usd + risk_usd
        cushion = self.bal0 + self.realized - self.floor()
        if cushion - p['K'] * orisk < p['buf']:
            return False, f'drawdown cushion ${cushion:.0f} too small for risk ${orisk:.0f}'
        if p['DLL'] and (p['DLL'] + self.realized) - orisk < p['buf']:
            return False, 'daily loss limit cushion'
        if p['selfstop'] and (p['selfstop'] + self.realized) - orisk < 0:
            return False, 'daily self-stop cushion'
        return True, ''

    def worst_hit(self, worst_open_usd):
        s = self.p['selfstop']
        return bool(s) and (self.realized + worst_open_usd) <= -s

    def end_day(self):
        bal = self.bal0 + self.realized
        self.bal0 = bal
        self.peak = max(self.peak, bal)
        self.realized = 0.0
        self.halted = False

    def state(self):
        return dict(bal0=self.bal0, peak=self.peak, floor=self.floor())


class Engine:
    def __init__(self, history, cfg=None, gate=None):
        """history: list of dicts {date,h,l,c} for completed sessions, oldest first."""
        self.cfg = {**DEFAULT, **(cfg or {})}
        self.hist = [dict(x) for x in history][-40:]
        self.gate = gate
        self.new_day(None)

    # ---------- day setup ----------
    def new_day(self, date):
        self.date = date
        self.o, self.h, self.l, self.c, self.v = [], [], [], [], []
        self.cumpv = 0.0; self.cumv = 0.0; self.vw = []
        self.st = {n: {'done': False} for n in self.cfg['strategies']}
        self.pos = None; self.pending = None
        self.trades = []
        self.halted = False
        self.fill_px = None
        self.notes = []
        if self.gate:
            self.gate.realized = 0.0; self.gate.halted = False
        H = self.hist
        self.ready = len(H) >= MIN_HISTORY
        self.reason = '' if self.ready else f'warming up: {len(H)}/{MIN_HISTORY} prior sessions'
        if date is not None and str(date) in set(self.cfg['skip_dates']):
            self.ready = False; self.reason = 'date in skip_dates (contract roll)'
        if len(H) >= 1:
            ranges = [x['h'] - x['l'] for x in H]
            closes = [x['c'] for x in H]
            self.pdh, self.pdl, self.pdc = H[-1]['h'], H[-1]['l'], H[-1]['c']
            self.dr = sum(ranges[-10:]) / len(ranges[-10:])
            sma = sum(closes[-20:]) / len(closes[-20:])
            self.trend = 1 if closes[-1] > sma else -1
            r7 = ranges[-7:]
            self.nr7 = len(r7) == 7 and r7[-1] == min(r7)
            self.prng = self.pdh - self.pdl

    # ---------- external hooks ----------
    def confirm_fill(self, price):
        """Optional: real fill price of the pending entry (live). Paper mode ignores this."""
        self.fill_px = price

    def force_flat(self, reason='broker flat'):
        """Broker says we are flat (stop hit at exchange, manual close...)."""
        if self.pos:
            self.notes.append(f'external flat: {reason}')
            self.pos = None
        self.pending = None

    # ---------- main entry ----------
    def on_bar(self, idx, o, h, l, c, v):
        ev = []
        self.o.append(o); self.h.append(h); self.l.append(l); self.c.append(c); self.v.append(v)
        assert len(self.c) == idx + 1, 'bars must arrive in order without gaps'
        tp = (h + l + c) / 3.0; w = max(v, 1)
        self.cumpv += tp * w; self.cumv += w; self.vw.append(self.cumpv / self.cumv)
        if not self.ready:
            return ev
        slip = self.cfg['slip']

        # 1) open a pending entry at this bar's open
        if self.pending and idx == self.pending['i0']:
            p = self.pending; self.pending = None
            ent = self.fill_px if self.fill_px is not None else o + p['d'] * slip
            self.fill_px = None
            self.pos = dict(p, ent=ent, stop=ent - p['d'] * p['stop_pts'],
                            tgt=None if p['tgt_pts'] is None else ent + p['d'] * p['tgt_pts'])
            ev.append(dict(type='ENTRY', idx=idx, strat=p['strat'], d=p['d'], price=ent,
                           stop=self.pos['stop'], tgt=self.pos['tgt']))

        # 2) exits for the open position (stop first, as in the backtest)
        if self.pos:
            ps = self.pos; d = ps['d']; res = None
            if d == 1:
                if l <= ps['stop']:
                    ex = min(ps['stop'], o) - slip; res = ((ex - ps['ent']), 'S')
                elif ps['tgt'] is not None and h >= ps['tgt']:
                    res = ((ps['tgt'] - ps['ent']), 'T')
            else:
                if h >= ps['stop']:
                    ex = max(ps['stop'], o) + slip; res = ((ps['ent'] - ex), 'S')
                elif ps['tgt'] is not None and l <= ps['tgt']:
                    res = ((ps['ent'] - ps['tgt']), 'T')
            if res is None and idx >= EXIT_IDX:
                ex = c - d * slip; res = (d * (ex - ps['ent']), 'X')
                ev.append(dict(type='FLATTEN', idx=idx, reason='time exit 15:55 bar'))
            if res is not None:
                pnl = (res[0] - COMM_RT / PV) * PV
                tr = dict(strat=ps['strat'], d=d, i0=ps['i0'], ex=idx, why=res[1], pnl=pnl)
                self.trades.append(tr)
                if self.gate:
                    self.gate.realized += pnl
                ev.append(dict(type='EXIT', idx=idx, **tr)); self.pos = None
            elif self.gate:
                worst = (l - ps['ent']) if d == 1 else (ps['ent'] - h)
                worst = max(worst, -ps['stop_pts']) * PV - COMM_RT
                if self.gate.worst_hit(worst):
                    pnl = worst
                    tr = dict(strat=ps['strat'], d=d, i0=ps['i0'], ex=idx, why='H', pnl=pnl)
                    self.trades.append(tr); self.gate.realized += pnl; self.gate.halted = True
                    self.pos = None; self.halted = True
                    ev.append(dict(type='FLATTEN', idx=idx, reason='daily self-stop'))
                    ev.append(dict(type='EXIT', idx=idx, **tr))

        # 3) new signals (decided on completed bar idx, executed at the open of idx+1)
        if idx < EXIT_IDX - 1 and not self.halted:
            for name in self.cfg['strategies']:
                if self.st[name]['done']:
                    continue
                sig = getattr(self, 'sig_' + name)(idx)
                if sig is None:
                    continue
                if sig == 'none':           # strategy has decided, no trade
                    continue
                d, stop_pts, tgt_pts = sig
                taken = dict(strat=name, d=d, i0=idx + 1, stop_pts=stop_pts, tgt_pts=tgt_pts)
                if self.cfg['one_at_a_time'] and (self.pos or self.pending):
                    ev.append(dict(type='SKIP', idx=idx, strat=name, reason='another position active')); continue
                if self.gate:
                    openr = 0.0
                    ok, why = self.gate.allow((stop_pts + self.cfg['slip']) * PV + COMM_RT, openr)
                    if not ok:
                        ev.append(dict(type='SKIP', idx=idx, strat=name, reason=why)); continue
                self.pending = taken
                ev.append(dict(type='SIGNAL', idx=idx, qty=self.cfg['qty'], **taken))
        return ev

    # ---------- strategy decisions ----------
    def _five(self, idx):
        return (idx + 1) % 5 == 0

    def sig_Gap(self, idx):
        if idx != 4:
            return None
        p = PARAMS['Gap']; self.st['Gap']['done'] = True
        gap = self.o[0] - self.pdc
        if abs(gap) < p['g'] * self.dr:
            return 'none'
        d = 1 if gap > 0 else -1
        if (self.c[4] - self.o[0]) * d < 0:
            return 'none'
        return (d, p['K'] * self.dr, None)

    def sig_VBgap(self, idx):
        if not self._five(idx) or idx < 9:
            return None
        kk = (idx + 1) // 5 - 1
        if kk > 49:
            self.st['VBgap']['done'] = True; return 'none'
        p = PARAMS['VBgap']; ci = self.c[idx]
        up = self.o[0] + p['k'] * self.prng; dn = self.o[0] - p['k'] * self.prng
        d = 1 if ci > up else (-1 if ci < dn else 0)
        if d == 0:
            return None
        self.st['VBgap']['done'] = True
        gap = self.o[0] - self.pdc
        if (gap > 0) != (d == 1):
            return 'none'
        return (d, p['K'] * self.dr, None)

    def _orb(self, idx, name, R, cap, minstop, need_nr7):
        if not self._five(idx) or idx < 14:
            return None
        kk = (idx + 1) // 5 - 1
        if kk > 29:
            self.st[name]['done'] = True; return 'none'
        if need_nr7 and not self.nr7:
            self.st[name]['done'] = True; return 'none'
        N = 15; orh = max(self.h[:N]); orl = min(self.l[:N]); ci = self.c[idx]
        d = 1 if (ci > orh and ci > self.vw[idx]) else (-1 if (ci < orl and ci < self.vw[idx]) else 0)
        if d == 0:
            return None
        self.st[name]['done'] = True
        if name == 'ORB' and d != self.trend:      # ORB is trend-filtered
            return 'none'
        stop = min(abs(ci - (orl if d == 1 else orh)), cap * self.dr)
        if stop < minstop:
            return 'none'
        slip = self.cfg['slip']
        return (d, stop + slip, R * stop)

    def sig_ORB(self, idx):
        p = PARAMS['ORB']; return self._orb(idx, 'ORB', p['R'], p['cap'], 0, False)

    def sig_NR7(self, idx):
        p = PARAMS['NR7']; return self._orb(idx, 'NR7', p['R'], p['cap'], p['minstop'], True)

    def sig_ORBR(self, idx):
        """ORB retest: first 5-min close outside the 15-min range (trend direction only); then wait up to `window` 5-min bars for price to come back to
        within tol*dr of the broken edge and a 5-min bar to CLOSE back beyond it; enter at the next open. Abandon if price closes back past the far side."""
        if not self._five(idx) or idx < 14:
            return None
        p = PARAMS['ORBR']; s = self.st['ORBR']; k = (idx + 1) // 5 - 1
        orh = max(self.h[:15]); orl = min(self.l[:15]); c = self.c[idx]
        if 'd' not in s:                                   # phase 1: look for the first breakout
            if k > 29:
                s['done'] = True; return 'none'
            d = 1 if (c > orh and c > self.vw[idx]) else (-1 if (c < orl and c < self.vw[idx]) else 0)
            if d == 0:
                return None
            if d != self.trend:
                s['done'] = True; return 'none'
            s['d'] = d; s['k0'] = k
            return None
        d = s['d']; j = k                                   # phase 2: look for the retest
        if j > s['k0'] + p['window'] or j >= 70:
            s['done'] = True; return 'none'
        edge = orh if d == 1 else orl; t = p['tol'] * self.dr
        if d == 1:
            if c < orl:
                s['done'] = True; return 'none'
            if min(self.l[idx - 4:idx + 1]) <= edge + t and c > edge:
                s['done'] = True; stop = min(abs(c - orl), p['cap'] * self.dr)
                return 'none' if stop < 4 else (1, stop + self.cfg['slip'], p['R'] * stop)
        else:
            if c > orh:
                s['done'] = True; return 'none'
            if max(self.h[idx - 4:idx + 1]) >= edge - t and c < edge:
                s['done'] = True; stop = min(abs(c - orh), p['cap'] * self.dr)
                return 'none' if stop < 4 else (-1, stop + self.cfg['slip'], p['R'] * stop)
        return None

    def sig_Trend30(self, idx):
        if idx != 29:
            return None
        p = PARAMS['Trend30']; self.st['Trend30']['done'] = True
        rng = max(self.h[:30]) - min(self.l[:30])
        if rng < p['m'] * self.dr:
            return 'none'
        loc = (self.c[29] - min(self.l[:30])) / rng
        d = 1 if loc >= 0.8 else (-1 if loc <= 0.2 else 0)
        if d == 0 or d != self.trend:
            return 'none'
        return (d, p['K'] * self.dr, None)

    # ---------- end of day ----------
    def finish_day(self):
        """Call after the last RTH bar. Appends today to history; returns a summary dict."""
        n = min(len(self.c), 389)
        out = dict(date=str(self.date), bars=len(self.c), trades=self.trades,
                   pnl=sum(t['pnl'] for t in self.trades), ready=self.ready, reason=self.reason)
        if n >= 380:
            self.hist.append(dict(date=str(self.date), h=max(self.h[:n]), l=min(self.l[:n]), c=self.c[n - 1]))
            self.hist = self.hist[-40:]
        else:
            out['reason'] = (out['reason'] + ' ').strip() + f'incomplete day ({n} bars), not added to history'
        if self.gate:
            self.gate.end_day(); out['gate'] = self.gate.state()
        return out


def load_history(path):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        return []


def save_history(path, hist):
    json.dump(hist[-40:], open(path, 'w'), indent=1)
