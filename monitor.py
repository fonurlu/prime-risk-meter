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


def fred(series):
    """Last value and date from FRED's public CSV (no API key needed)."""
    if series not in _fred:
        r = requests.get(f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}', headers=UA, timeout=30)
        r.raise_for_status()
        rows = [row for row in csv.reader(io.StringIO(r.text))][1:]
        rows = [(d, float(v)) for d, v in rows if v not in ('.', '')]
        _fred[series] = rows[-1]
    d, v = _fred[series]
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

    rows = []
    for ind in cfg['indicators']:
        stale = False
        try:
            if OFFLINE and ind['source']['type'] not in ('manual',):
                raise RuntimeError('offline')
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
    latest = dict(updated=now.strftime('%Y-%m-%d %H:%M ET'), updated_iso=now.isoformat(), gauge=gauge,
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
