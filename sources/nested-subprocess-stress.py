"""Guest probe: concurrent Python subprocess launches as performed by the guest agent.

Without user/group changes CPython 3.10 launches with vfork; with them it uses fork.
"""
import argparse
import collections
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import traceback


def launch(mode, count):
    errors = collections.Counter()
    first = None
    for _ in range(count):
        kwargs = {'user': 0, 'group': 0, 'extra_groups': []} if mode == 'fork' else {}
        try:
            process = subprocess.Popen(['/bin/true'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True, **kwargs)
            out, err = process.communicate(timeout=60)
            if process.returncode or out or err:
                errors['unexpected result'] += 1
        except Exception as error:
            errors[f'{type(error).__name__}: {error}'] += 1
            first = first or traceback.format_exc()
    return errors, first


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('vfork', 'fork'), required=True)
    parser.add_argument('--threads', type=int, default=16)
    parser.add_argument('--launches', type=int, default=200)
    args = parser.parse_args()
    with ThreadPoolExecutor(args.threads) as pool:
        results = list(pool.map(lambda _: launch(args.mode, args.launches), range(args.threads)))
    errors = collections.Counter()
    for counter, _ in results:
        errors.update(counter)
    first = next((trace for _, trace in results if trace), None)
    print(json.dumps({'mode': args.mode, 'launches': args.threads * args.launches,
                      'errors': dict(errors), 'first_traceback': first}, indent=2))
    raise SystemExit(1 if errors else 0)


if __name__ == '__main__':
    main()
