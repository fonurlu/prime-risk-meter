"""PRIME Risk Meter - runs every 15 minutes on GitHub Actions.

Fetches 26 market, credit and economic indicators, scores each 0 (calm) to 100 (danger),
combines them into one gauge, writes docs/data/*.json for the web page, and opens a
GitHub issue (GitHub emails the owner) when something crosses a danger line.
"""
import csv, io, json, os, sys, datetime as dt
from zoneinfo import ZoneInfo
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'docs', 'data')
ET = ZoneInfo('America/New_York')
UA = {'User-Agent': 'Mozilla/5.0 (PRIME risk meter; personal use)'}
OFFLINE = '--offline' in sys.argv          # test mode: no network, reuse last values
NO_ALERTS = '--no-alerts' in sys.argv


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(obj, f, indent=1)


# ---------------------------------------------------------------- data sources
_fred, _yahoo = {}, {}


FRED_CACHE = os.path.join(DATA, 'fred_cache.json')
_fred_cache = None


def fred(series):
    """Last value and date for a FRED series. Uses the free FRED API key (GitHub secret FRED_API_KEY)
    when present, else FRED's public CSV. Economic data changes daily at most, so each series is
    re-fetched only when the cached copy is older than 4 hours."""
    global _fred_cache
    if _fred_cache is None:
        _fred_cache = load(FRED_CACHE, {})
    c = _fred_cache.get(series)
    now = dt.datetime.now(dt.timezone.utc)
    if c and (now - dt.datetime.fromisoformat(c['fetched'])).total_seconds() < 4 * 3600:
        return c['value'], c['date']
    key = os.environ.get('FRED_API_KEY')
    if key:
        r = requests.get('https://api.stlouisfed.org/fred/series/observations', timeout=25, headers=UA,
                         params={'series_id': series, 'api_key': key, 'file_type': 'json', 'sort_order': 'desc', 'limit': 10})
        r.raise_for_status()
        obs = [o for o in r.json()['observations'] if o['value'] not in ('.', '')]
        d, v = obs[0]['date'], float(obs[0]['value'])
    else:
        r = requests.get(f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}', headers=UA, timeout=25)
        r.raise_for_status()
        rows = [row for row in csv.reader(io.StringIO(r.text))][1:]
        d, v = [(a, float(b)) for a, b in rows if b not in ('.', '')][-1]
    _fred_cache[series] = {'value': v, 'date': d, 'fetched': now.isoformat()}
    save(FRED_CACHE, _fred_cache)
    return v, d


def yahoo(sym):
    """Daily closes for the last ~13 months, with the latest intraday price as the last point."""
    if sym not in _yahoo:
        r = requests.get(f'https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(sym)}',
                         params={'range': '13mo', 'interval': '1d'}, headers=UA, timeout=30)
        r.raise_for_status()
        res = r.json()['chart']['result'][0]
        closes = [c for c in res['indicators']['quote'][0]['close'] if c is not None]
        live = res['meta'].get('regularMarketPrice')
        t = res['meta'].get('regularMarketTime')
        if live:
            closes[-1] = live
        _yahoo[sym] = (closes, dt.datetime.fromtimestamp(t, ET).strftime('%d %b %H:%M ET') if t else '')
    return _yahoo[sym]


def cnn_fear_greed():
    r = requests.get('https://production.dataviz.cnn.io/index/fearandgreed/graphdata', headers=UA, timeout=30)
    r.raise_for_status()
    fg = r.json()['fear_and_greed']
    return round(float(fg['score']), 1), dt.datetime.fromisoformat(fg['timestamp'].replace('Z', '+00:00')).astimezone(ET).strftime('%d %b')


def measure(src, manual):
    t = src['type']
    if t == 'fred':
        v, d = fred(src['id'])
        return v, f"FRED {src['id']} {d}"
    if t == 'manual':
        m = manual[src['key']]
        return m['value'], f"manual · {m['asof']}"
    if t == 'cnn_fg':
        try:
            v, d = cnn_fear_greed()
            return v, f'CNN {d}'
        except Exception:
            m = manual[src['key']]
            return m['value'], f"manual · {m['asof']}"
    if t == 'last':
        c, d = yahoo(src['sym'])
        return round(c[-1], 2), f"Yahoo {src['sym']} {d}"
    if t == 'vs_ma':
        c, d = yahoo(src['sym'])
        ma = sum(c[-src['n']:]) / src['n']
        return round((c[-1] / ma - 1) * 100, 2), f"Yahoo {src['sym']} {d}"
    if t == 'drawdown':
        c, d = yahoo(src['sym'])
        return round((c[-1] / max(c[-252:]) - 1) * 100, 2), f"Yahoo {src['sym']} {d}"
    if t == 'ratio_chg':
        a, d = yahoo(src['a']); b, _ = yahoo(src['b'])
        n = src['days']
        return round(((a[-1] / b[-1]) / (a[-1 - n] / b[-1 - n]) - 1) * 100, 2), f"Yahoo {src['a']}/{src['b']} {d}"
    if t == 'ratio_last':
        a, d = yahoo(src['a']); b, _ = yahoo(src['b'])
        return round(a[-1] / b[-1], 3), f"Yahoo {src['a']}/{src['b']} {d}"
    raise ValueError(t)


# ---------------------------------------------------------------- agenda auto-fill
AGENDA = os.path.join(DATA, 'agenda.json')
FRED_RULES = [  # (words in the event title, FRED series, mode, scale, decimals)
    ('us retail sales', 'RSAFS', 'pct', 1, 1), ('industrial production', 'INDPRO', 'pct', 1, 1),
    ('housing starts', 'HOUST', 'level', 1, 0), ('jobless claims', 'ICSA', 'level', 0.001, 0),
    ('michigan', 'UMCSENT', 'level', 1, 1)]
TICKERS = {'home depot': 'HD', 'disney': 'DIS', 'target': 'TGT', 'lowe': 'LOW', 'tjx': 'TJX', 'walmart': 'WMT', 'ross': 'ROST',
           'nvidia': 'NVDA', 'intuit': 'INTU', 'palo alto': 'PANW', 'coreweave': 'CRWV', 'salesforce': 'CRM', 'zoom': 'ZM'}


def fred_obs(series, n=8):
    """Fresh (uncached) latest observations, oldest first: [(date, value)]."""
    key = os.environ.get('FRED_API_KEY')
    if key:
        r = requests.get('https://api.stlouisfed.org/fred/series/observations', timeout=25, headers=UA,
                         params={'series_id': series, 'api_key': key, 'file_type': 'json', 'sort_order': 'desc', 'limit': n})
        r.raise_for_status()
        return [(o['date'], float(o['value'])) for o in r.json()['observations'] if o['value'] not in ('.', '')][::-1]
    r = requests.get(f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}', headers=UA, timeout=25)
    r.raise_for_status()
    return [(a, float(b)) for a, b in list(csv.reader(io.StringIO(r.text)))[1:] if b not in ('.', '')][-n:]


def _due(e):
    """When the release should be out: 15 min after the clock time, 09:35 for pre-open, next session 09:35 for after-close."""
    y, mo, d = map(int, e['date'].split('-'))
    t = e.get('time', '')
    if len(t) == 5 and t[2] == ':':
        return dt.datetime(y, mo, d, int(t[:2]), int(t[3:]), tzinfo=ET) + dt.timedelta(minutes=15)
    if t == 'Pre-open':
        return dt.datetime(y, mo, d, 9, 35, tzinfo=ET)
    if t == 'After close':
        nxt = dt.datetime(y, mo, d, 9, 35, tzinfo=ET) + dt.timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += dt.timedelta(days=1)
        return nxt
    return None


def _ref(series, event_date):
    """Reference period the release covers: weekly claims = latest Saturday before the date, else the previous month."""
    d = dt.date.fromisoformat(event_date)
    if series == 'ICSA':
        d -= dt.timedelta(days=(d.weekday() + 2) % 7 or 7)
        return d.isoformat()
    first = d.replace(day=1) - dt.timedelta(days=1)
    return first.replace(day=1).isoformat()


def autofill_agenda(now):
    """As soon as a release is out, fill 'actual' (official data via FRED) or the earnings price reaction,
    so the page never sits on 'Released - data pending'. Events can still carry a manual 'actual'/'result'."""
    ag = load(AGENDA, None)
    if not ag:
        return
    filled, errs = [], []
    for e in ag['events']:
        if e.get('actual') is not None or e.get('result') or e.get('result_note'):
            continue
        due = _due(e)
        if not due or now < due:
            continue
        title = e['event'].lower()
        try:
            rule = next((r for r in FRED_RULES if r[0] in title), None)
            if rule:
                _, ser, mode, scale, dp = rule
                ref = _ref(ser, e['date'])
                obs = dict(fred_obs(ser))
                dates = sorted(obs)
                if ref not in obs:
                    continue                      # not on FRED yet - try again next run
                v = obs[ref]
                if mode == 'pct':
                    i = dates.index(ref)
                    if i == 0:
                        continue
                    v = (v / obs[dates[i - 1]] - 1) * 100
                e['actual'] = round(v * scale, dp)
                e['actual_src'] = f"FRED {ser} {ref[:7]}, auto {now.strftime('%d %b %H:%M ET')}"
                filled.append(e['event'])
            elif any(w in title for w in ('results', 'earnings')):
                syms = [t for k, t in TICKERS.items() if k in title]
                if not syms:
                    continue
                parts = []
                for sym in syms:
                    c, _ = yahoo(sym)
                    parts.append(f"{sym} {(c[-1] / c[-2] - 1) * 100:+.1f}%")
                e['result'] = 'reported'
                e['result_note'] = 'Share reaction vs prior close (auto, ' + now.strftime('%d %b %H:%M ET') + '): ' + ', '.join(parts) + '. Results detail: company release.'
                filled.append(e['event'])
        except Exception as ex:
            errs.append(f"{e['event'][:30]}: {str(ex)[:80]}")
    ag['auto_checked'] = now.strftime('%Y-%m-%d %H:%M ET')
    ag['auto_errors'] = errs
    if filled:
        ag['updated'] = ag['auto_checked']
    save(AGENDA, ag)
    print('agenda auto-fill:', filled or 'nothing due', errs or '')


# ---------------------------------------------------------------- scoring
def score(v, benign, danger):
    return max(0.0, min(100.0, (v - benign) / (danger - benign) * 100))


def zone(g, cfg):
    for lo, hi, name in cfg['zones']:
        if lo <= g < hi:
            return name
    return cfg['zones'][-1][2]


def run():
    cfg = load(os.path.join(ROOT, 'config.json'), None)
    manual = load(os.path.join(ROOT, 'manual.json'), {})
    prev = load(os.path.join(DATA, 'latest.json'), {})
    prev_ind = {r['id']: r for r in prev.get('indicators', [])}
    now = dt.datetime.now(ET)

    # warm the caches in parallel (FRED and Yahoo), so one slow source can't stall the run
    from concurrent.futures import ThreadPoolExecutor
    jobs = set()
    for ind in cfg['indicators']:
        s = ind['source']
        if s['type'] == 'fred': jobs.add(('fred', s['id']))
        for k in ('sym', 'a', 'b'):
            if k in s and s['type'] != 'fred': jobs.add(('yahoo', s[k]))
    def warm(j):
        try:
            (fred if j[0] == 'fred' else yahoo)(j[1]); return j, None
        except Exception as e:
            return j, str(e)[:120]
    errors = {}
    if not OFFLINE:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for j, err in ex.map(warm, sorted(jobs)):
                if err: errors[f'{j[0]}:{j[1]}'] = err
    for k, v in errors.items():
        print('fetch failed', k, v, flush=True)

    rows = []
    for ind in cfg['indicators']:
        stale = False
        try:
            src_t = ind['source']
            if OFFLINE and src_t['type'] not in ('manual',):
                raise RuntimeError('offline')
            if src_t['type'] == 'fred' and f"fred:{src_t['id']}" in errors:
                raise RuntimeError('fetch failed')
            if any(f'yahoo:{src_t[k]}' in errors for k in ('sym', 'a', 'b') if k in src_t):
                raise RuntimeError('fetch failed')
            v, src = measure(ind['source'], manual)
        except Exception as e:
            p = prev_ind.get(ind['id'])
            if not p:
                print('no data for', ind['id'], e)
                continue
            v, src, stale = p['value'], p['source'].replace(' (stale)', '') + ' (stale)', True
        s = score(v, ind['benign'], ind['danger'])
        rows.append(dict(id=ind['id'], pillar=ind['pillar'], name=ind['name'], value=v, benign=ind['benign'],
                         danger=ind['danger'], score=round(s, 1), source=src, stale=stale, why=ind['why']))

    pillars = []
    for key, p in cfg['pillars'].items():
        ss = [r['score'] for r in rows if r['pillar'] == key]
        pillars.append(dict(key=key, name=p['name'], weight=p['weight'], score=round(sum(ss) / len(ss), 1) if ss else None))
    ok = [p for p in pillars if p['score'] is not None]
    gauge = round(sum(p['weight'] * p['score'] for p in ok) / sum(p['weight'] for p in ok))
    pmap = {p['key']: p['score'] for p in pillars}
    contrarian = pmap['sent'] >= 50 and (pmap['macro'] + pmap['credit']) / 2 < 30
    horizon = [
        dict(name='Correction (-10%) within 3 months', score=round(0.5 * pmap['sent'] + 0.5 * pmap['trend'])),
        dict(name='Bear market (-20%) within 6 months', score=round(0.5 * pmap['trend'] + 0.5 * pmap['credit'])),
        dict(name='Recession within 12 months', score=round(0.6 * pmap['macro'] + 0.4 * pmap['credit'])),
    ]
    latest = dict(fetch_errors=errors, updated=now.strftime('%Y-%m-%d %H:%M ET'), updated_iso=now.isoformat(), gauge=gauge,
                  zone=zone(gauge, cfg), contrarian=contrarian, pillars=pillars, horizon=horizon, indicators=rows)

    # history: one point per run for 3 days, then one per day (the day's last reading)
    hist = load(os.path.join(DATA, 'history.json'), [])
    hist.append(dict(t=now.strftime('%Y-%m-%d %H:%M'), g=gauge, **{k: pmap[k] for k in pmap}))
    cutoff = (now - dt.timedelta(days=3)).strftime('%Y-%m-%d')
    recent = [h for h in hist if h['t'][:10] >= cutoff]
    daily = {}
    for h in hist:
        if h['t'][:10] < cutoff:
            daily[h['t'][:10]] = h
    hist = list(daily.values()) + recent

    alerts(cfg, latest, prev, hist, now)
    if not OFFLINE:
        try:
            autofill_agenda(now)
        except Exception as ex:
            print('agenda auto-fill failed:', ex)
    save(os.path.join(DATA, 'latest.json'), latest)
    save(os.path.join(DATA, 'history.json'), hist)
    print(f"gauge {gauge} {latest['zone']} | " + ' '.join(f"{p['key']}={p['score']}" for p in pillars) +
          f" | stale={sum(r['stale'] for r in rows)}")


# ---------------------------------------------------------------- alerts (GitHub issues -> email)
def gh(method, path, **kw):
    tok, repo = os.environ.get('GITHUB_TOKEN'), os.environ.get('GITHUB_REPOSITORY')
    if not tok or not repo:
        print('[no GitHub token] would', method, path, kw.get('json', {}).get('title', ''))
        return None
    r = requests.request(method, f'https://api.github.com/repos/{repo}{path}', timeout=30,
                         headers={'Authorization': f'Bearer {tok}', 'Accept': 'application/vnd.github+json'}, **kw)
    r.raise_for_status()
    return r.json()


def alerts(cfg, latest, prev, hist, now):
    a = cfg['alerts']
    state = load(os.path.join(DATA, 'alert_state.json'), {'active': {}, 'sent_today': {}, 'day': ''})
    day = now.strftime('%Y-%m-%d')
    if state.get('day') != day:
        state['sent_today'], state['day'] = {}, day
    quiet = now.hour >= a['quiet_start_hour_et'] or now.hour < a['quiet_end_hour_et']
    ind = {r['id']: r for r in latest['indicators']}

    conditions = {}   # key -> message, for everything that is "on" now
    for r in latest['indicators']:
        if r['score'] >= a['indicator_score'] and not r['stale']:
            conditions[f"ind:{r['id']}"] = f"{r['name']} = {r['value']} (score {round(r['score'])}/100). {r['why']}."
    for m in a['market_lines']:
        r = ind.get(m['id'])
        if r and not r['stale'] and ((m['op'] == '>' and r['value'] > m['value']) or (m['op'] == '<' and r['value'] < m['value'])):
            conditions[f"line:{m['id']}"] = f"{m['label']}: now {r['value']}."
    if latest['zone'] != 'Green':
        conditions[f"zone:{latest['zone']}"] = f"PRIME Risk Gauge is in the {latest['zone']} zone ({latest['gauge']})."
    # 10-point jump vs the reading ~24h ago
    old = [h for h in hist if h['t'] <= (now - dt.timedelta(hours=24)).strftime('%Y-%m-%d %H:%M')]
    if old and latest['gauge'] - old[-1]['g'] >= a['jump_points']:
        conditions['jump'] = f"Gauge jumped {latest['gauge'] - old[-1]['g']} points in 24h ({old[-1]['g']} -> {latest['gauge']})."

    red = latest['zone'] == 'Red'
    new = {k: v for k, v in conditions.items() if k not in state['active'] and k not in state['sent_today']}
    cleared = [k for k in state['active'] if k not in conditions]

    if NO_ALERTS:
        new, cleared = {}, []
    if new and (not quiet or red):
        lines = '\n'.join(f'- {v}' for v in new.values())
        top = list(new.values())[0]
        body = (f"**PRIME Risk Gauge: {latest['gauge']} ({latest['zone']})** at {latest['updated']}\n\n{lines}\n\n"
                f"Pillars: " + ', '.join(f"{p['name']} {round(p['score'])}" for p in latest['pillars']) +
                f"\n\nContrarian flag: {'ON' if latest['contrarian'] else 'off'}\n\nOpen the meter: https://{os.environ.get('GITHUB_REPOSITORY_OWNER','')}.github.io/{os.environ.get('GITHUB_REPOSITORY','/').split('/')[-1]}/")
        issue = gh('POST', '/issues', json={'title': f"PRIME Risk ALERT: {top[:90]}", 'body': body})
        for k, v in new.items():
            state['active'][k] = {'msg': v, 'issue': issue['number'] if issue else None, 'since': latest['updated']}
            state['sent_today'][k] = latest['updated']
    elif new:
        print('quiet hours - holding', list(new))
    if cleared and not quiet:
        for k in cleared:
            info = state['active'].pop(k)
            if info.get('issue'):
                gh('POST', f"/issues/{info['issue']}/comments", json={'body': f"All clear: {info['msg']} is back to normal (gauge {latest['gauge']}, {latest['updated']})."})
                still = [x for x in state['active'].values() if x.get('issue') == info['issue']]
                if not still:
                    gh('PATCH', f"/issues/{info['issue']}", json={'state': 'closed'})
    save(os.path.join(DATA, 'alert_state.json'), state)


if __name__ == '__main__':
    run()
