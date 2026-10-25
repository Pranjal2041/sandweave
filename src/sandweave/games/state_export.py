#!/usr/bin/env python3
"""Live state of a frontier game, read inside the guest from the game's own telemetry.

Each game environment already writes trusted, root-only telemetry for its
verifier. This reads it incrementally (the cursor and aggregate are cached
between calls, so a growing journal is parsed once) and prints one JSON object:
{"game", "state", "guest_ns", "source", "new_bytes"}. guest_ns is the guest's
CLOCK_MONOTONIC when the read finished, the clock of the sandbox's frames.

    python3 state_export.py GAME [--source PATH] [--cache DIR] [--reader PATH]

Sources (defaults are the environments' own paths):
  speed-dreams  race journal (sd-2.4.2-eval-journal.patch): session, sim time and
                multiplier, the human car, standings, laps, session results
  open-saber    gameplay journal (open_saber_journal.patch; a session's private
                directory selects its newest journal): map, score, combo, counts
  openttd       game log "GACR" records (gamescript/main.nut): date, companies,
                quarters, events, setting changes, pause, end
  wesnoth       audit plugin lines in game.log (audit_plugin.lua): context,
                scenario, turn, level results, debug mode
  openra        newest replay, read with the environment's openra_replay.py:
                game time, players, orders by kind and by client
Standard library only; runs as root because the telemetry is root-only.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time

DEFAULT_SOURCES = {'speed-dreams': '/var/lib/sdracer/eval/journal.jsonl',
                   'openttd': '/var/lib/ottd_task/run/game.log',
                   'wesnoth': '/var/lib/wesnoth-audit/game.log',
                   'openra': '/var/lib/openra/support/Replays',
                   'open-saber': None}
SABER_COUNTED = ('note_spawn', 'note_cut', 'note_miss', 'bomb_spawn', 'bomb_hit', 'bomb_pass',
                 'wall_spawn', 'wall_enter', 'wall_exit', 'wall_pass')
WESNOTH_EVENT = re.compile(r'^\[ga-audit\] event seq=(\d+) time=(\d+) turn=(\S+) result=(\S+) scenario=(.*)$')
WESNOTH_CONTEXT = re.compile(r'^\[ga-audit\] context time=(\d+) name=(.*)$')
HISTORY = 100


def speed_dreams(state, lines):
    state = state or {'session': None, 'cars': {}, 'sim': None, 'humans': [], 'standings': [], 'laps': {},
                      'multipliers': [], 'results': {}, 'last_event': None, 'records': 0}
    for line in lines:
        record = json.loads(line)
        event = record.get('ev')
        state['records'] += 1
        state['last_event'] = event
        state['sim'] = {key: record.get(key) for key in ('sid', 't', 'mult', 'state', 'mono')}
        if event == 'session':
            track = record.get('track') or {}
            state['session'] = {**{key: record.get(key) for key in ('sid', 'session', 'type', 'laps', 'duration',
                                                                    'raceman')},
                                'track': {key: track.get(key) for key in ('name', 'length')}}
            state['cars'] = {str(car['idx']): car.get('name') for car in record.get('cars', [])}
            state['laps'] = {}
        elif event == 'h':
            state['humans'] = record.get('cars', [])
        elif event == 'f':
            names = ('idx', 'lap', 'pos', 'd', 'x', 'y', 'v', 'state', 'dmg', 'fuel')
            cars = [{**dict(zip(names, car)), 'name': state['cars'].get(str(car[0]))} for car in record.get('cars', [])]
            state['standings'] = sorted(cars, key=lambda car: car['pos'])
        elif event == 'lap':
            state['laps'][str(record['idx'])] = {key: record.get(key) for key in ('lap', 'time', 'best', 'valid',
                                                                                  'pos', 'splits')}
        elif event == 'timemult':
            state['multipliers'] = (state['multipliers'] + [state['sim']])[-HISTORY:]
        elif event == 'end':
            state['results'][str(record.get('session'))] = record.get('cars')
    return state


def open_saber(state, lines):
    state = state or {'session': None, 'map': None, 'active': False, 'counts': {}, 'score': None, 'combo': None,
                      'multiplier': None, 'song_t': None, 'pose': None, 'results': [], 'records': 0}
    for line in lines:
        # Each line is "<json>\t<hmac>"; the verifier checks the chain, this only reads it.
        record = json.loads(line.split('\t', 1)[0])
        kind = record.get('type')
        state['records'] += 1
        if 'song_t' in record:
            state['song_t'] = record['song_t']
        if kind == 'session_start':
            state['session'] = {key: record.get(key) for key in ('format', 'executable_sha256')}
        elif kind == 'map_start':
            state['map'] = {key: value for key, value in record.items()
                            if key not in ('settings', 'camera_local', 'prev', 'seq', 'mono_us', 't', 'type')}
            state.update(active=True, counts={}, score=0, combo=0, multiplier=1)
        elif kind in SABER_COUNTED:
            state['counts'][kind] = state['counts'].get(kind, 0) + 1
            state.update({key: record[key] for key in ('score', 'combo', 'multiplier') if key in record})
        elif kind == 'pose':
            state['pose'] = {key: record.get(key) for key in ('h', 'l', 'r')}
        elif kind == 'map_end':
            state['active'] = False
            state['results'].append({key: record.get(key) for key in ('attempt', 'score', 'percent', 'max_combo',
                                                                      'right_notes', 'wrong_notes', 'full_combo')})
    return state


def openttd(state, lines):
    state = state or {'date': None, 'paused': None, 'companies': [], 'quarters': [], 'events': [],
                      'settings_changed': [], 'ended': None, 'session': None, 'records': 0}
    for line in lines:
        index = line.find('GACR ')
        if index < 0:
            continue
        record = json.loads(line[index + 5:])
        kind = record.get('k')
        state['records'] += 1
        state['date'] = record.get('date', state['date'])
        if kind in ('hello', 'start', 'load'):
            state['session'] = {'record': kind, 'token': record.get('tok'), 'date': record.get('date')}
        elif kind == 'm':
            state.update(companies=record.get('co', []), paused=record.get('paused'))
        elif kind == 'q':
            state['quarters'] = (state['quarters'] + [{'q': record.get('q'), 'companies': record.get('co')}])[-24:]
        elif kind == 'evt':
            state['events'] = (state['events'] + [{key: value for key, value in record.items()
                                                    if key not in ('tok', 'seq', 'k', 'sys', 'tick')}])[-HISTORY:]
        elif kind == 'set':
            state['settings_changed'].append({key: record.get(key) for key in ('name', 'old', 'val', 'date')})
        elif kind == 'pause':
            state['paused'] = record.get('paused')
        elif kind == 'end':
            state['ended'] = {key: value for key, value in record.items() if key not in ('tok', 'seq', 'k')}
    return state


def wesnoth(state, lines):
    state = state or {'context': None, 'scenario': None, 'turn': None, 'result': None, 'scenarios': {},
                      'game_starts': 0, 'debug_mode': False, 'final': None, 'events': 0}
    for line in lines:
        line = line.rstrip()
        event, context = WESNOTH_EVENT.match(line), WESNOTH_CONTEXT.match(line)
        if line == '[ga-audit] plugin started':
            state['game_starts'] += 1
        elif context:
            state['context'] = context[2]
        elif event:
            turn = int(event[3]) if event[3].isdigit() else event[3]
            state.update(scenario=event[5], turn=turn, result=event[4])
            state['events'] += 1
            scenario = state['scenarios'].setdefault(event[5], {'first_seen': int(event[2]), 'results': []})
            scenario['turn'] = turn
            if event[4] != 'NONE':
                scenario['results'].append({'result': event[4], 'turn': turn, 'time': int(event[2])})
        elif line.startswith('[ga-audit] debug-mode'):
            state['debug_mode'] = True
        elif line.startswith('[ga-audit] final'):
            state['final'] = line[len('[ga-audit] final '):]
    return state


def openra(source, reader):
    sys.path.insert(0, str(Path(reader).parent))
    import openra_replay
    replays = sorted(Path(source).rglob('*.orarep'), key=lambda path: path.stat().st_mtime)
    if not replays:
        return None, {'replays': 0}
    replay = openra_replay.Replay.load(str(replays[-1]))
    summary = openra_replay.summarize(replay)
    orders, _ = replay.orders()
    by_client = {}
    for order in orders:
        client = str(order.get('client'))
        by_client.setdefault(client, {})
        by_client[client][order['order']] = by_client[client].get(order['order'], 0) + 1
    interval = int((replay.lobby().get('global', {}).get('fields', {}).get('NetFrameInterval') or 3))
    return str(replays[-1]), {
        'replays': len(replays), 'last_frame': summary['last_frame'],
        # Net frames x NetFrameInterval x 40 ms at Normal speed, as the task's scoring computes game time.
        'game_seconds': summary['last_frame'] * interval * 0.04, 'finished': bool(summary['metadata']),
        'players': summary['lobby'].get('clients', []), 'orders': summary['order_counts'],
        'orders_by_client': by_client, 'truncated': summary['truncated']}


REDUCERS = {'speed-dreams': speed_dreams, 'open-saber': open_saber, 'openttd': openttd, 'wesnoth': wesnoth}


def journal(game, source):
    path = Path(source)
    if game == 'open-saber' and path.is_dir():
        journals = sorted(path.glob('journal-*.jsonl'))
        if not journals:
            raise FileNotFoundError('no Open Saber journal in ' + source)
        path = journals[-1]
    return path


def read_new(path, cache):
    """Complete lines added since the cached offset; a replaced or truncated file restarts."""
    status = path.stat()
    if cache.get('path') != str(path) or cache.get('inode') != status.st_ino or status.st_size < cache.get('offset', 0):
        cache.clear()
        cache.update(path=str(path), inode=status.st_ino, offset=0, state=None)
    with open(path, 'rb') as source:
        source.seek(cache['offset'])
        data = source.read()
    end = data.rfind(b'\n') + 1
    cache['offset'] += end
    return data[:end].decode('utf-8', 'replace').splitlines(), end


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('game', choices=sorted(DEFAULT_SOURCES))
    parser.add_argument('--source')
    parser.add_argument('--cache', default='/run/sandweave-state')
    parser.add_argument('--reader', default='/workspace/utils/openra_replay.py')
    args = parser.parse_args()
    source = args.source or DEFAULT_SOURCES[args.game]
    if source is None:
        parser.error(args.game + ' needs --source')
    if args.game == 'openra':
        path, state = openra(source, args.reader)
        new_bytes = None
    else:
        path = journal(args.game, source)
        cache_dir = Path(args.cache)
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_file = cache_dir / f'{args.game}-{hashlib.sha256(source.encode()).hexdigest()[:16]}.json'
        cache = json.loads(cache_file.read_text()) if cache_file.is_file() else {}
        lines, new_bytes = read_new(path, cache)
        cache['state'] = state = REDUCERS[args.game](cache.get('state'), lines)
        temporary = cache_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(cache))
        temporary.replace(cache_file)
    print(json.dumps({'game': args.game, 'state': state, 'guest_ns': time.monotonic_ns(), 'source': str(path),
                      'new_bytes': new_bytes}, separators=(',', ':')))


if __name__ == '__main__':
    main()
