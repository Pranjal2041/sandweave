"""The `state` observation: a game's live state from the telemetry its environment already writes.

    from sandweave.games.state import GameState
    state = GameState('speed-dreams')
    state.observe(sandbox)          # {'game', 'state', 'guest_ns', 'source', 'new_bytes'}
    steps.run(sandbox, step, state=state)

The guest exporter (state_export.py) is installed once per sandbox under a
root-only directory and run as root, because the telemetry is root-only. Its
cursor and aggregate are cached in the guest, so each call parses only what the
game wrote since the previous one. Works through the public SDK on any runtime.
"""
from importlib import resources
import json

GAMES = ('open-saber', 'openra', 'openttd', 'speed-dreams', 'wesnoth')
DIRECTORY = '/run/sandweave-state'
SCRIPT = DIRECTORY + '/state_export.py'


class GameState:
    def __init__(self, game, *, source=None, reader=None, resim=False):
        if game not in GAMES:
            raise ValueError(f'unknown game {game!r}; known: {", ".join(GAMES)}')
        if game == 'open-saber' and source is None:
            raise ValueError("open-saber needs source: the session's private directory or a journal")
        if resim and game != 'openra':
            raise ValueError('resim applies to openra')
        self.game, self.source, self.reader, self.resim = game, source, reader, resim
        self.script = resources.files(__package__).joinpath('state_export.py').read_bytes()
        self.installed = set()

    def command(self):
        argv = ['python3', SCRIPT, self.game]
        if self.source is not None:
            argv += ['--source', str(self.source)]
        if self.reader is not None:
            argv += ['--reader', str(self.reader)]
        return argv + (['--resim'] if self.resim else [])

    def observe(self, sandbox):
        if sandbox.id not in self.installed:
            sandbox.run(f'install -d -m 700 {DIRECTORY}', user='root', check=True)
            sandbox.files.write_bytes(SCRIPT, self.script, mode=0o700)
            self.installed.add(sandbox.id)
        result = sandbox.run(argv=self.command(), user='root', check=True)
        return json.loads(result.stdout)
