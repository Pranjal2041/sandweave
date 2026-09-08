#!/usr/bin/env python3
"""Check Unicode cache turnover in an accelerated Firefox address bar."""
import argparse
import json
from pathlib import Path
import time
import urllib.request

from cua_harness_adapter import GVisorXvncAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', help='fresh fastio-harness-* environment')
    parser.add_argument('--gpu', default='0')
    args = parser.parse_args()
    backend = GVisorXvncAdapter(options={'name': args.name, 'gpu': args.gpu})
    output = Path('runs') / args.name
    output.mkdir(parents=True, exist_ok=False)

    def guest(command, timeout=120):
        code, text = backend.exec(command, timeout=timeout)
        if code:
            raise RuntimeError(text)
        return text

    def wait(fn, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = fn()
                if result:
                    return result
            except (OSError, RuntimeError):
                pass
            time.sleep(.1)
        raise TimeoutError('Firefox acceptance timed out')

    try:
        backend.boot()
        backend.copy_in('scripts/fast-io-input-probe.py', '/opt/fast-io-input-probe.py')
        guest('systemd-run --unit=fastio-probe --uid=ga --setenv=DISPLAY=:1 '
              '--setenv=XAUTHORITY=/home/ga/.Xauthority python3 /opt/fast-io-input-probe.py')
        port = backend.manager.status(args.name)['ports']['8000']

        def state():
            with urllib.request.urlopen(f'http://127.0.0.1:{port}', timeout=3) as response:
                return json.load(response)

        wait(state)
        guest('install -d -o ga -g ga /home/ga/fastio-firefox')
        prefs = output/'user.js'
        prefs.write_text('\n'.join([
            'user_pref("browser.shell.checkDefaultBrowser", false);',
            'user_pref("browser.aboutwelcome.enabled", false);',
            'user_pref("browser.startup.homepage_override.mstone", "ignore");',
            'user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);',
        ])+'\n')
        backend.copy_in(prefs, '/home/ga/fastio-firefox/user.js')
        guest('chown ga:ga /home/ga/fastio-firefox/user.js')
        guest('systemd-run --unit=fastio-firefox --uid=ga --setenv=HOME=/home/ga '
              '--setenv=DISPLAY=:1 --setenv=XAUTHORITY=/home/ga/.Xauthority '
              '--setenv=MOZ_X11_EGL=0 /usr/local/bin/engine-gpu-gl '
              'firefox --no-remote --profile /home/ga/fastio-firefox http://127.0.0.1:8000/gpu')
        gpu = wait(lambda: state().get('gpu'), timeout=90)
        assert 'NVIDIA' in gpu['renderer'], gpu
        client = backend.client
        read_clipboard = ('python3 -c "import tkinter as t; r=t.Tk(); r.withdraw(); '
                          'print(r.clipboard_get(), end=\'\')"')
        client.action({'keyboard': {'keys': ['ctrl','l']}})
        # Address-bar focus is an application transition, not covered by the
        # X-server input ACK. Settle and verify this setup before the stress.
        time.sleep(.5)
        client.action({'keyboard': {'text': 'setup'}})
        client.action({'keyboard': {'keys': ['ctrl','a']}})
        client.action({'keyboard': {'keys': ['ctrl','c']}})
        wait(lambda: guest(read_clipboard) == 'setup')
        expected = ''
        for i in range(64):
            text = ''.join(chr(0x4e00+i*16+j) for j in range(16))
            client.action({'keyboard': {'text': text}})
            expected += text
        client.action({'keyboard': {'keys': ['ctrl','a']}})
        client.action({'keyboard': {'keys': ['ctrl','c']}})
        # Clipboard is an observation only; every typed character used XTEST.
        observed = wait(lambda: guest(read_clipboard) if guest(read_clipboard) != 'setup' else None, timeout=10)
        (output/'expected.txt').write_text(expected)
        (output/'observed.txt').write_text(observed)
        assert observed == expected, {'expected_len':len(expected), 'got_len':len(observed)}
        client.screenshot().save(output/'firefox.png')
        report = {'passed': True, 'renderer': gpu['renderer'], 'distinct_codepoints': len(expected),
                  'exact_address_bar_match': True, 'clipboard_used_only_to_observe': True}
        (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2))
    except BaseException:
        try:
            (output/'guest-diagnostics.txt').write_text(guest('journalctl --no-pager -u fastio-firefox -n 80'))
            backend.client.screenshot().save(output/'failure.png')
        except Exception:
            pass
        raise
    finally:
        backend.teardown()


if __name__ == '__main__':
    main()
