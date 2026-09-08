#!/usr/bin/env python3
"""Unicode turnover, stalled consumers and lifecycle on a disposable GTK desktop."""
import argparse
import json
from pathlib import Path
import statistics
import time
import urllib.request

from cua_harness_adapter import GVisorXvncAdapter
from fast_io import detach


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', help='fresh fastio-harness-* environment')
    args = parser.parse_args()
    backend = GVisorXvncAdapter(options={'name': args.name})
    manager = backend.manager
    output = Path('runs') / args.name
    output.mkdir(parents=True, exist_ok=False)
    report = {}
    clone = args.name + '-clone'

    def guest(command):
        code, text = backend.exec(command)
        if code:
            raise RuntimeError(text)
        return text

    def wait(fn, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = fn()
                if result:
                    return result
            except (OSError, RuntimeError):
                pass
            time.sleep(.05)
        raise TimeoutError('GTK acceptance timed out')

    def state(name=args.name):
        port = manager.status(name)['ports']['8000']
        with urllib.request.urlopen(f'http://127.0.0.1:{port}', timeout=3) as response:
            return json.load(response)

    try:
        backend.boot()
        source = Path('scripts/fast-io-input-probe.py').read_text()
        backend.copy_in('scripts/fast-io-input-probe.py', '/usr/local/bin/fastio-gtk-probe.py')
        guest('systemd-run --unit=fastio-keymap-probe --setenv=DISPLAY=:1 '
              '--setenv=XAUTHORITY=/home/ga/.Xauthority python3 /usr/local/bin/fastio-gtk-probe.py')
        first = wait(state)
        wait(lambda: guest(f'xdotool search --onlyvisible --pid {first["pid"]}'))
        client = backend.client
        expected = ''
        elapsed = []
        # No application reads or sleeps between these public action calls.
        for i in range(64):
            text = ''.join(chr(0x4e00 + i*16 + j) for j in range(16))
            tick = time.perf_counter()
            client.action({'keyboard': {'text': text}})
            elapsed.append((time.perf_counter() - tick)*1000)
            expected += text
        wait(lambda: state()['text'] == expected)
        report['unicode'] = {'distinct_codepoints': len(expected), 'exact_match': True,
                             'action_median_ms': statistics.median(elapsed),
                             'action_p95_ms': sorted(elapsed)[60]}
        # More distinct unmapped keys than fit in one batch must reject all of it.
        try:
            client.action([{'keyboard': {'text': 'must-not-arrive'}},
                           {'keyboard': {'text': ''.join(chr(0x6000+i) for i in range(32))}}])
        except RuntimeError as error:
            assert 'one batch exceeds' in str(error), error
        else:
            raise AssertionError('oversized Unicode batch accepted')
        assert state()['text'] == expected
        report['oversized_batch_atomic'] = True

        second_path = output/'second-gtk-probe.py'
        second_path.write_text(source.replace("('0.0.0.0', 8000)", "('0.0.0.0', 8001)")
                               .replace("title='Fast I/O acceptance'", "title='Fast IO second'")
                               .replace('window.move(80, 90)', 'window.move(1010, 90)'))
        backend.copy_in(second_path, '/usr/local/bin/fastio-gtk-second.py')
        guest('systemd-run --unit=fastio-keymap-second --setenv=DISPLAY=:1 '
              '--setenv=XAUTHORITY=/home/ga/.Xauthority python3 /usr/local/bin/fastio-gtk-second.py')
        second = wait(lambda: json.loads(guest('curl -fsS http://127.0.0.1:8001')))
        window = wait(lambda: guest("xdotool search --name '^Fast IO second$'").splitlines()[0])
        guest(f'xdotool windowactivate --sync {window}')
        guest(f'xdotool windowmove --sync {window} 1010 90 windowraise {window} windowfocus --sync {window}')
        client.action({'mouse': {'left_click': [1080,190]}})
        client.action({'keyboard': {'text': 'setup'}})
        wait(lambda: json.loads(guest('curl -fsS http://127.0.0.1:8001'))['text'] == 'setup')
        client.action({'keyboard': {'keys': ['ctrl','a']}})
        client.action({'keyboard': {'keys': 'backspace'}})
        wait(lambda: json.loads(guest('curl -fsS http://127.0.0.1:8001'))['text'] == '')
        # The previous consumer must still be fenced after focus moves away.
        guest(f'kill -STOP {first["pid"]}')
        next_text = ''.join(chr(0x6100+i) for i in range(16))
        tick = time.perf_counter()
        try:
            try:
                client.action({'keyboard': {'text': next_text}})
            except RuntimeError as error:
                assert '_NET_WM_PING' in str(error), error
                report['stalled_previous_consumer'] = {'rejected': True, 'elapsed_s': time.perf_counter()-tick}
            else:
                raise AssertionError('recycled mappings while previous consumer was stopped')
            assert json.loads(guest('curl -fsS http://127.0.0.1:8001'))['text'] == ''
        finally:
            guest(f'kill -CONT {first["pid"]}')
        client.action({'keyboard': {'text': next_text}})
        wait(lambda: json.loads(guest('curl -fsS http://127.0.0.1:8001'))['text'] == next_text)
        assert state()['text'] == expected
        report['retry_after_stall'] = True

        # A held temporary keysym must survive a rejected detach, then release.
        client.action({'keyboard': {'keys_down': next_text[-1]}})
        try:
            detach(manager, args.name)
        except RuntimeError as error:
            assert 'release temporary Unicode keys' in str(error), error
        else:
            raise AssertionError('detached with a held temporary key')
        client.action({'keyboard': {'keys_up': next_text[-1]}})
        second_before_save = json.loads(guest('curl -fsS http://127.0.0.1:8001'))
        report['held_unicode_detach_recovery'] = True
        client.screenshot().save(output/'gtk.png')
        generation = client.last_metadata['generation']
        manager.pause(args.name)
        manager.resume(args.name)
        client.screenshot()
        assert client.last_metadata['generation'] != generation
        assert state()['text'] == expected
        report['pause_resume'] = True
        saved = manager.save(args.name, args.name+'-live', mode='live', local_only=True)
        manager.load(saved['snapshot'], clone)
        restored = wait(lambda: state(clone))
        assert restored['text'] == expected and restored['nonce'] == first['nonce']
        with manager.fast_io(clone) as restored_client:
            restored_client.action({'keyboard': {'text': ' 👋🏽'}})
            def restored_second():
                return json.loads(manager._run([*manager._command(clone), 'exec', clone,
                    'curl', '-fsS', 'http://127.0.0.1:8001']))
            wait(lambda: restored_second()['text'] == second_before_save['text'] + ' 👋🏽')
            assert restored_second()['nonce'] == second['nonce']
            restored_client.screenshot().save(output/'restored.png')
        report['live_restore'] = {'snapshot': saved['snapshot'], 'text_and_nonce_preserved': True}
        report['passed'] = True
        (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2))
    except BaseException:
        (output/'partial-report.json').write_text(json.dumps(report, indent=2)+'\n')
        try:
            (output/'guest-diagnostics.txt').write_text(guest('xprop -root _NET_CLIENT_LIST; '
                'xprop -root _NET_ACTIVE_WINDOW; xdotool getwindowfocus; '
                'journalctl --no-pager -u fastio-keymap-probe -u fastio-keymap-second -n 60'))
            (output/'first-state.json').write_text(json.dumps(state(), indent=2)+'\n')
            (output/'second-state.json').write_text(guest('curl -fsS http://127.0.0.1:8001'))
            backend.client.screenshot().save(output/'failure.png')
        except Exception:
            pass
        raise
    finally:
        if manager.status(clone)['status'] != 'missing':
            manager.stop(clone, discard=True)
        backend.teardown()


if __name__ == '__main__':
    main()
