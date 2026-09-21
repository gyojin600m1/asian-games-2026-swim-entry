#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第20回アジア競技大会 競泳（2026-09-20〜25 東京アクアティクスセンター）速報の見張り。

  ・公式結果サイトの裏API（back.results.asiangames2026.org）を本線にする。
      schedule/daily/{日}   … 組ごとの予定時刻・状態（SCHEDULED → START_LIST → … → OFFICIAL）
      results/{組のKey}      … スタートリスト（レーン・申込タイム）→ 結果（記録・順位・Q・記録印）
  ・予選は本人の行に 組・レーンと記録を付ける。決勝は本人の行を増やして付ける。リレーは泳者も入れる。
  ・順位は、その種目の組が全部終わってから 記録順に付け直す（1組ごとの順位は出さない）。
  ・検算（build.py）に落ちたら公開しない。
  ・launchd から1分おきに動く（競技のある時間帯だけ）。Claudeは使わない。
"""
import json, os, re, subprocess, sys, time, copy, collections

HERE   = os.path.dirname(os.path.abspath(__file__))
APP    = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from api import get

BASE   = os.path.join(HERE, 'base.json')                       # 公開時のエントリー（不変）
STATE  = os.path.join(HERE, 'state.json')                      # 取り込んだ組の内容
SRC    = os.path.expanduser('~/swim-entry-app/samples/アジア競技大会2026競泳.json')
BUILD  = os.path.expanduser('~/swim-entry-app/build.py')
OUT    = os.path.expanduser('~/swim-entry-app/out/アジア競技大会2026競泳/index.html')
BACKUP = os.path.expanduser('~/swim-apps-backup/apps/asian-games-2026-swim-entry/index.html')
LOG    = os.path.join(HERE, 'log.txt')
HALT   = os.path.join(HERE, '.halted')
LABEL  = 'com.fukuda.swim.asian-games-2026'

DAYS   = ['2026-09-20', '2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24', '2026-09-25']
DAYLBL = {d: f'{i+1}日目' for i, d in enumerate(DAYS)}
WINDOW = ('09:30', '19:59')                                     # 予選10:00〜／決勝17:00〜19:00ごろ
STOP_AFTER = '2026-09-25 20:30'
RECHECK = 15 * 60                                               # 終わった組は15分に1回だけ見直す（訂正対応）
FINAL_N = {'A': 10, 'T': 8}                                    # 決勝は個人10名（0〜9レーン）・リレー8チーム（9/20の決勝スタートリストで確認）

SHORT = {'中華人民共和国': '中国', 'ホンコン・チャイナ': '香港', '大韓民国': '韓国', 'イラン・イスラム共和国': 'イラン',
         'ラオス人民民主共和国': 'ラオス', '東ティモール民主共和国': '東ティモール', 'マカオ・チャイナ': 'マカオ'}
IRM_JA = {'DSQ': '失格', 'DNS': '棄権', 'DNF': '途中棄権', 'DQ': '失格', 'WDR': '棄権', 'EXH': '参考'}
REC_JA = {'WR': '世界新', 'AR': 'アジア新', 'AS': 'アジア新', 'GR': '大会新', 'NR': '国内新', 'WJ': '世界Jr新', 'CR': '大会新'}
STROKE = {'FR': '自由形', 'BA': '背泳ぎ', 'BR': '平泳ぎ', 'BF': 'バタフライ', 'IM': '個人メドレー'}
GEN    = {'W': '女子', 'M': '男子', 'X': '混合'}


def log(m):
    with open(LOG, 'a') as f:
        f.write(time.strftime('%m/%d %H:%M ') + m + '\n')


def notify(title, msg):
    subprocess.run(['osascript', '-e',
        f'display notification "{msg[:200].replace(chr(34), chr(39)).replace(chr(10), " ")}" '
        f'with title "{title.replace(chr(34), chr(39))}" sound name "Glass"'], check=False)


def stop_myself():
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], check=False)


def log_once(state, m):
    """同じ知らせを毎回書かない（state.logged に覚える）"""
    seen = state.setdefault('logged', [])
    if m not in seen:
        seen.append(m); log(m)


def load(p, d):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return d


def sec(t):
    m = re.match(r'^(?:(\d+):)?(\d+)\.(\d+)$', str(t or '').strip())
    return (int(m.group(1) or 0)) * 60 + int(m.group(2)) + float('0.' + m.group(3)) if m else None


# ---------- 種目Key → プログラム ----------
def ev_parts(evkey):
    g = GEN[evkey[0]]
    body = evkey[2:].rstrip('-')
    m = re.match(r'(4X)?(\d+)M(FR|BA|BR|BF|IM|MD)$', body)
    if m.group(1):
        return g, '4×%sm' % m.group(2), ('フリーリレー' if m.group(3) == 'FR' else 'メドレーリレー')
    return g, '%sm' % m.group(2), STROKE[m.group(3)]


def unit_parts(key):
    """'W.200MBF------------.HEAT.000100--' → (evkey, phase, unitnum)"""
    evkey, phase, unit = key[:20], key[21:25], key[26:]
    m = re.match(r'(\d{6})', unit)                       # 000100 → 第1組, 000200 → 第2組
    return evkey, phase, (int(m.group(1)) // 100 if m else 0)


def program_no(prog_index, key):
    """組のKeyから、その組が入るプログラムNo.を返す（無ければ None）"""
    evkey, phase, un = unit_parts(key)
    g, dist, stroke = ev_parts(evkey)
    timed = dist in ('800m', '1500m')
    if phase == 'HEAT':
        rnd = '予選'
    elif phase.startswith('FNL'):
        if timed:
            rnd = 'タイム決勝②' if (un == 1 or phase == 'FNL1') else 'タイム決勝①'
        else:
            rnd = '決勝'
    else:
        return None
    return prog_index.get((g, dist, stroke, rnd))


def compact(unit):
    """results/{組} の応答を、必要な項目だけに削る"""
    info = unit.get('Info') or {}
    rows = []
    for c in unit.get('Competitors') or []:
        ext = {e.get('Code'): e.get('Value') for e in c.get('Extensions') or []}
        row = {'reg': c.get('Reg'), 'org': c.get('Org'), 'orgdesc': c.get('OrgDesc'), 'name': c.get('Name'),
               'lane': c.get('Lane'), 'res': (c.get('Result') or '').strip(), 'rk': c.get('Rk'),
               'irm': c.get('IRM') or 'OK', 'q': c.get('Qualified') or '', 'rec': c.get('RecordInd') or '',
               'qt': ext.get('QT') or ''}
        mem = []
        for m in c.get('Members') or []:
            mem.append({'reg': m.get('Reg'), 'name': m.get('Name')})
        if mem:
            row['members'] = mem
        rows.append(row)
    rows.sort(key=lambda r: (str(r.get('lane') or ''), str(r.get('reg') or '')))   # RUNNING中は並びが変わるので、並びの違いを変更扱いにしない
    return {'status': info.get('Status') or '', 'type': info.get('Type') or 'A', 'unitnum': info.get('UnitNum'),
            'rows': rows, 'at': time.strftime('%H:%M')}


def unit_done(u):
    """組が終わったか（記録か棄権/失格が全員に付いている）"""
    if not u or not u['rows'] or u['status'] in ('SCHEDULED', 'START_LIST'):
        return False
    return all(r['res'] or r['irm'] != 'OK' for r in u['rows'])


# ---------- 取り込み ----------
def fetch(state, byhand):
    """予定と組を取りに行く。state を更新し、中身が変わったら True。
       今日の分は毎回、未来の日は10分に1回、過去の日は1時間に1回（終わった組は15分に1回）。"""
    today = time.strftime('%Y-%m-%d')
    sched = state.setdefault('schedule', {})
    units = state.setdefault('units', {})
    now = time.time()
    changed = False
    for day in DAYS:
        last = sched.get(day, {}).get('_at', 0)
        every = 0 if day == today else (600 if day > today else 3600)
        if sched.get(day) and now - last < every and not byhand:
            continue
        d = get(f'/s/AG2026/en/SWM/schedule/daily/{day}')
        if not isinstance(d, list):
            log(f'  日程が取れない {day}: {str(d)[:80]}')
            continue
        new_units = [{'key': u.get('Key'), 'status': u.get('Status'), 'time': (u.get('DateTimeRaw') or '')[11:16],
                      'partics': (u.get('Counters') or {}).get('Partics') or 0}
                     for u in d if u.get('Key') and ('.HEAT.' in u['Key'] or '.FNL' in u['Key'])]
        if json.dumps(new_units) != json.dumps((sched.get(day) or {}).get('units')):
            changed = True
        sched[day] = {'_at': now, 'units': new_units}
    # 組の中身
    for day in DAYS:
        for su in (sched.get(day) or {}).get('units') or []:
            key = su['key']
            if su['status'] == 'SCHEDULED' and not su['partics']:
                continue                                   # スタートリスト前
            cur = units.get(key)
            if cur and not byhand:
                if unit_done(cur) and cur['status'] == su['status'] and now - cur.get('_at', 0) < RECHECK:
                    continue                               # 終わった組は15分に1回
                if day != today and now - cur.get('_at', 0) < 600:
                    continue                               # 今日以外の未終了の組は10分に1回
            d = get(f'/s/AG2026/ja/SWM/results/{key}')
            if not isinstance(d, dict) or 'Competitors' not in d:
                continue
            c = compact(d); c['_at'] = now
            if not c['rows']:
                continue
            old = units.get(key)
            if not old or json.dumps(old['rows'], ensure_ascii=False) != json.dumps(c['rows'], ensure_ascii=False) or old['status'] != c['status']:
                if not old or old['status'] != c['status']:
                    log(f'  {key} {c["status"]} {len(c["rows"])}件' + ('' if not unit_done(c) else ' ✓終了'))
                changed = True
            units[key] = c
    return changed


# ---------- データへの反映 ----------
def merge(base, state):
    data = copy.deepcopy(base)
    meta, prog = data['meta'], data['program']
    prog_index = {(p['gender'], p['distance'], p['stroke'], p['round']): p['no'] for p in prog}
    PBN = {p['no']: p for p in prog}
    sw_name = {s['id']: s['name'] for s in data['swimmers'] if s.get('id')}
    sw_team = {s['id']: s['team'] for s in data['swimmers'] if s.get('id')}

    # 1) 予定時刻・組数（公式の日程）
    ptimes, pheats = collections.defaultdict(list), collections.defaultdict(int)
    for day, sd in (state.get('schedule') or {}).items():
        for su in sd.get('units') or []:
            no = program_no(prog_index, su['key'])
            if no is None:
                continue
            if su['time']:
                ptimes[no].append(su['time'])
            pheats[no] += 1
    for p in prog:
        if ptimes.get(p['no']):
            p['startTime'] = min(ptimes[p['no']])
        if pheats.get(p['no']):
            p['heats'] = pheats[p['no']]

    # 2) 組ごとの内容
    units = state.get('units') or {}
    entries, relays = data['entries'], data['relays']
    heat_rows = {}                                    # (id, evkey) → 予選の行（決勝の行を作る型）
    org_team = {}                                     # NOC → チーム名（日本語）
    for e in entries:
        if e.get('id'):
            heat_rows.setdefault(e['id'], {})
    def find_entry(reg, no):
        for e in entries:
            if e.get('id') == reg and no in (e.get('programNos') or []):
                return e
        return None
    def find_relay(team, no):
        for r in relays:
            if r['team'] == team and no in (r.get('programNos') or []):
                return r
        return None
    def team_of(row):
        t = row.get('orgdesc') or row['org']
        return SHORT.get(t, t)

    n_res = 0
    done_prog = collections.defaultdict(list)          # no → [unit_done, …]
    finished_nos = set()
    for key, u in sorted(units.items()):
        no = program_no(prog_index, key)
        if no is None:
            continue
        p = PBN[no]
        evkey, phase, un = unit_parts(key)
        g, dist, stroke = ev_parts(evkey)
        timed = dist in ('800m', '1500m')
        is_final = p['round'] != '予選'
        heat_no = prog_index.get((g, dist, stroke, '予選')) or prog_index.get((g, dist, stroke, 'タイム決勝①'))
        done = unit_done(u)
        done_prog[no].append(done)
        heatnum = un or int(u.get('unitnum') or 0)          # Info.UnitNum は当てにならない（全部 "1" が返る）。Keyの 000300 → 第3組
        for r in u['rows']:
            if '4X' in evkey:
                # ---- リレー ----
                team = team_of(r); org_team[r['org']] = team
                row = find_relay(team, no)
                if row is None:
                    src = find_relay(team, heat_no)
                    if src is None and not is_final:
                        # 申込一覧に無かったチーム（追加エントリー）
                        row = {'team': team, 'gender': g, 'distance': dist, 'stroke': stroke, 'programNos': [no]}
                        relays.append(row)
                        log_once(state, f'  申込一覧に無いリレーを追加 {team} {evkey}')
                    elif src is None:
                        log_once(state, f'  リレーが名簿に無い {team} {evkey}')
                        continue
                    else:
                        row = {k: v for k, v in src.items() if k not in ('heat', 'lane', 'result', 'note')}
                        row['programNos'] = [no]
                        relays.append(row)
                if r.get('members'):
                    row['members'] = [sw_name.get(m['reg'], m['name']) for m in r['members']]
            else:
                # ---- 個人 ----
                row = find_entry(r['reg'], no)
                if row is None and timed and p['round'] == 'タイム決勝②':
                    row = find_entry(r['reg'], heat_no)          # ①に紐づいていた行を②へ移す
                    if row is not None:
                        row['programNos'] = [no]
                if row is None and is_final:
                    src = find_entry(r['reg'], heat_no)
                    if src is None:
                        log_once(state, f'  選手が名簿に無い {r["name"]} {evkey}')
                        continue
                    row = {k: v for k, v in src.items() if k not in ('heat', 'lane', 'result', 'note', 'time', 'sortTime')}
                    row['programNos'] = [no]
                    if src.get('note') and '出身' in src['note']:
                        row['note'] = src['note'].split('・')[0].strip()
                    entries.append(row)
                if row is None and not is_final:
                    # 申込一覧に無かった選手（追加エントリー）。名簿にも足す
                    sw = next((x for x in data['swimmers'] if x.get('id') == r['reg']), None)
                    if sw is None:
                        sw = {'id': r['reg'], 'name': r['name'], 'team': team_of(r), 'gender': g if g != '混合' else '男子'}
                        data['swimmers'].append(sw)
                        log_once(state, f'  名簿に無い選手を追加 {r["name"]}（{sw["team"]}）')
                    row = dict(sw); row.update({'distance': dist, 'stroke': stroke, 'programNos': [no]})
                    entries.append(row)
                    log_once(state, f'  申込一覧に無い出場者を追加 {r["name"]} No.{no}')
                if row is None:
                    log_once(state, f'  行が見つからない {r["name"]} {key}')
                    continue
            # 組・レーン・申込タイム
            lane = r.get('lane')
            try:
                lane = int(lane)
            except Exception:
                lane = None
            row['heat'] = heatnum
            if lane is not None:
                row['lane'] = lane
            if r.get('qt'):
                row['time'] = r['qt']; row['sortTime'] = sec(r['qt'])
            base_note = ''
            if row.get('note') and '出身' in row['note']:
                base_note = row['note'].split('・')[0].strip()
            ln = f'第{heatnum}組 {lane}レーン' if lane is not None else ''
            row['note'] = ' ・ '.join(x for x in (base_note, ln) if x)
            # 記録
            res = None
            if r['irm'] != 'OK':
                res = {'note': IRM_JA.get(r['irm'], r['irm'])}
            elif r['res']:
                res = {'time': r['res']}
                if r.get('rec'):
                    res['rec'] = REC_JA.get(r['rec'], r['rec'])
            if res:
                row['result'] = res; n_res += 1
                row['_q'] = r.get('q') or ''
                row['_done'] = done
            elif 'result' in row:
                del row['result']

    # 3) 順位: その種目（プログラム）の組が全部終わったら、記録順に付け直す
    for no, flags in done_prog.items():
        if not flags or not all(flags):
            continue
        p = PBN[no]
        rows = [x for x in entries + relays if no in (x.get('programNos') or []) and x.get('result')]
        ok = [x for x in rows if x['result'].get('time') and sec(x['result']['time']) is not None]
        ok.sort(key=lambda x: sec(x['result']['time']))
        rank, prev = 0, None
        for i, x in enumerate(ok):
            t = sec(x['result']['time'])
            if t != prev:
                rank = i + 1; prev = t
            x['result']['rank'] = rank
        finished_nos.add(no)
        if p['round'] == '予選':
            relay = '×' in (p.get('distance') or '')
            # 決勝のスタートリストが出ていれば、それに載っている人を「決勝へ」。まだなら記録順の上位（個人10・リレー8）
            fno = prog_index.get((p['gender'], p['distance'], p['stroke'], '決勝'))
            fin_keys = {(x.get('id') or x.get('team')) for x in entries + relays if fno in (x.get('programNos') or []) and (x.get('lane') is not None)}
            qs = [x for x in ok if str(x.get('_q', '')).upper().startswith('Q')]
            if fin_keys:
                adv = [x for x in ok if (x.get('id') or x.get('team')) in fin_keys]
            else:
                adv = qs if qs else ok[:FINAL_N['T' if relay else 'A']]
            for x in adv:
                x['result']['adv'] = True
    # タイム決勝①②は両方が終わってから、①②合わせて順位を付ける
    for p in prog:
        if p['round'] != 'タイム決勝②':
            continue
        no2 = p['no']; no1 = prog_index.get((p['gender'], p['distance'], p['stroke'], 'タイム決勝①'))
        f1, f2 = done_prog.get(no1), done_prog.get(no2)
        rows = [x for x in entries if (no1 in (x.get('programNos') or []) or no2 in (x.get('programNos') or [])) and x.get('result')]
        if f1 and f2 and all(f1) and all(f2):
            ok = [x for x in rows if x['result'].get('time') and sec(x['result']['time']) is not None]
            ok.sort(key=lambda x: sec(x['result']['time']))
            rank, prev = 0, None
            for i, x in enumerate(ok):
                t = sec(x['result']['time'])
                if t != prev:
                    rank = i + 1; prev = t
                x['result']['rank'] = rank
        else:
            for x in rows:
                x['result'].pop('rank', None)
    for x in entries + relays:
        x.pop('_q', None); x.pop('_done', None)

    # 3b) 検算値（追加エントリー等で件数が動くので実データから作り直し、元との差をログに残す）
    ind_uniq = {(e.get('id') or e['name'], e.get('team'), e.get('distance'), e.get('stroke')) for e in entries if e.get('entryType') != 'リレーのみ'}
    rel_uniq = {(r.get('team'), r.get('gender'), r.get('distance'), r.get('stroke')) for r in relays}
    teams = {s.get('team') for s in data['swimmers'] if s.get('team')} | {e.get('team') for e in entries if e.get('team')}
    for r in relays:
        n = r.get('team')
        if n and n not in teams and re.sub(r'[ 　]?[A-ZＡ-Ｚ]$', '', n) not in teams:
            teams.add(n)
    counts = {'individualEntries': len(ind_uniq), 'relayOnlySwimmers': sum(1 for e in entries if e.get('entryType') == 'リレーのみ'),
              'relayEntries': len(rel_uniq), 'swimmers': len(data['swimmers']), 'programItems': len(prog), 'schoolsAndTeams': len(teams)}
    old_counts = (data.get('summary') or {}).get('counts') or {}
    diff = {k: (old_counts.get(k), v) for k, v in counts.items() if old_counts.get(k) != v}
    if diff:
        log_once(state, '  検算値が動いた: ' + json.dumps(diff, ensure_ascii=False))
    data.setdefault('summary', {})['counts'] = counts

    # 4) 画面の設定
    any_lane = any(x.get('lane') is not None for x in entries + relays)
    if any_lane:
        meta['lanePrediction'] = True
        meta['laneConfirmed'] = True
        meta.pop('orderLabel', None)
    n_fin = len(finished_nos)
    if n_res:
        meta['liveLabel'] = '速報中'
        last = None
        for key, u in units.items():
            if unit_done(u):
                no = program_no(prog_index, key)
                if no is not None and (last is None or no > last):
                    last = no
        lp = PBN.get(last)
        jp = [s for s in data['swimmers'] if s['team'] == '日本']
        meta['notice'] = (f'🏁 結果を反映中 — {n_fin}/82 プログラム終了' +
                          (f'（最新 No.{lp["no"]} {lp["gender"]}{lp["distance"]}{lp["stroke"]} {lp["round"]}）' if lp else '') +
                          f'\n🇯🇵 日本代表 **{len(jp)}名**。⭐が日本代表、🎓が鹿児島の高校出身の選手です。')
    return data, n_res, n_fin


merge.last_diff = None


def publish(data, n_res, n_fin):
    # 画面に出る中身が前回の公開と同じなら（組の状態が変わっただけ等）何もしない
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    try:
        if open(SRC, encoding='utf-8').read() and json.dumps(json.load(open(SRC, encoding='utf-8')), ensure_ascii=False, sort_keys=True) == payload:
            return True
    except Exception:
        pass
    json.dump(data, open(SRC, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    b = subprocess.run(['python3', BUILD, SRC], capture_output=True, text=True)
    if b.returncode != 0 or not os.path.exists(OUT):
        log('  ビルドが検算に落ちた: ' + (b.stdout + b.stderr)[-400:].replace('\n', ' / '))
        notify('アジア大会 競泳', 'ビルドが検算に落ちました（公開せず）')
        return False
    subprocess.run(['cp', OUT, os.path.join(APP, 'index.html')])
    with open(os.path.join(APP, 'live.json'), 'w') as f:
        json.dump({'n': n_res, 't': time.strftime('%H:%M')}, f)
    try:
        os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
        subprocess.run(['cp', OUT, BACKUP])
    except Exception:
        pass
    subprocess.run(['git', 'add', 'index.html', 'live.json'], cwd=APP, capture_output=True)
    msg = f'速報を反映（{n_fin}/82プログラム・結果{n_res}件） {time.strftime("%H:%M")}'
    cm = subprocess.run(['git', '-c', 'user.name=gyojin600m1', '-c', 'user.email=gyojin600m1@gmail.com',
                         'commit', '-q', '-m', msg], cwd=APP, capture_output=True, text=True)
    if cm.returncode != 0:
        return True                                    # 変更なし
    ps = subprocess.run(['git', 'push', '-q'], cwd=APP, capture_output=True, text=True)
    log(f'  {msg} → ' + ('公開した' if ps.returncode == 0 else 'pushに失敗（次回やり直す）: ' + ps.stderr[-120:]))
    return ps.returncode == 0


def main():
    now = time.strftime('%Y-%m-%d %H:%M')
    if now > STOP_AFTER:
        log('大会が終わったので見張りを止めた')
        notify('アジア大会 競泳', '見張りを終了しました')
        stop_myself()
        return
    if os.path.exists(HALT):
        return
    byhand = '--now' in sys.argv
    today, hm = now[:10], now[11:]
    if not byhand and (today not in DAYS or not (WINDOW[0] <= hm <= WINDOW[1])):
        return
    base = load(BASE, None)
    if not base:
        log('base.json が無い'); return
    # 競技中（予選10:00〜12:30／決勝17:00〜19:30）は1回の起動で20秒おきに3回見る＝実質20秒間隔
    in_session = (not byhand) and (('09:55' <= hm <= '12:30') or ('16:55' <= hm <= '19:30'))
    for i in range(3 if in_session else 1):
        if i:
            time.sleep(20)
        state = load(STATE, {})
        changed = fetch(state, byhand)
        json.dump(state, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False)
        if changed or byhand:
            data, n_res, n_fin = merge(base, state)
            json.dump(state, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False)
            publish(data, n_res, n_fin)


if __name__ == '__main__':
    import fcntl
    _lock = open(os.path.join(HERE, '.lock'), 'w')
    try:
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)
    try:
        main()
    except Exception as ex:
        import traceback
        log('想定外のエラー: ' + repr(ex)[:200] + ' / ' + traceback.format_exc().splitlines()[-2][:120])
        raise
