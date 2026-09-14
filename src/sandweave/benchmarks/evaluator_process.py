"""Private evaluator process; stdout is reserved for framed JSON results."""
import contextlib
import json
from pathlib import Path
import sys
import traceback
import types


def main():
    replies = sys.stdout
    path = Path(sys.argv[1])
    with contextlib.redirect_stdout(sys.stderr):
        module = types.ModuleType('sandweave_osworld_verifier')
        module.__file__ = str(path)
        exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
        module.ensure_osworld_evaluators()
    if len(sys.argv) > 2 and sys.argv[2] == '--check':
        return
    print(json.dumps({'ready': True}), file=replies, flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                value = module.check_with_source(Path(request['source']), request['trajectory'],
                                                  request['env_info'], {})
            response = {'result': value}
        except Exception:
            response = {'error': traceback.format_exc()}
        print(json.dumps(response), file=replies, flush=True)


if __name__ == '__main__':
    main()
