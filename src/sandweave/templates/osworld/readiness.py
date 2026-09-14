"""Run the upstream setup unchanged, waiting for its desktop applications.

This file runs inside the original OSWorld image. A background process being
alive is insufficient: GIMP and LibreOffice can take longer to map a document
window. Check each launch before subsequent setup actions can target it.
"""
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time


APPLICATIONS = {
    'gimp': 'gimp', 'google-chrome': 'google-chrome',
    'google-chrome-stable': 'google-chrome', 'chromium': 'chromium',
    'firefox': 'firefox', 'code': 'code', 'thunderbird': 'thunderbird',
    'nautilus': 'org.gnome.nautilus', 'vlc': 'vlc',
    'gnome-terminal': 'gnome-terminal', 'gedit': 'gedit',
}


def expectation(item):
    """Identify a GUI launch without mistaking background helpers for windows."""
    parameters = item.get('parameters') or {}
    if item.get('type') == 'open':
        path = Path(parameters['path'])
        extension = path.suffix.lower()
        family = ('libreoffice' if extension in {
            '.xlsx', '.xls', '.ods', '.csv', '.pptx', '.ppt', '.odp',
            '.docx', '.doc', '.odt', '.rtf'} else '')
        return {'application': family, 'document': path.stem}
    if item.get('type') != 'launch':
        return None
    command = parameters.get('command', [])
    words = shlex.split(command) if isinstance(command, str) else command
    # Shell assignments such as VLC_VERBOSE=-1 precede the executable.
    words = list(words)
    while words and '=' in words[0] and not words[0].startswith('/'):
        words.pop(0)
    if not words:
        return None
    application = APPLICATIONS.get(Path(words[0]).name)
    if application is None:
        return None
    documents = [Path(word).stem for word in words[1:] if word.startswith('/home/')]
    return {'application': application, 'document': documents[-1] if documents else ''}


def matches(window, expected):
    return (window['normal'] and window['visible']
            and expected['application'].casefold() in window['application'].casefold()
            and expected['document'].casefold() in window['title'].casefold())


def windows(display):
    from Xlib import X, error
    root = display.screen().root
    atom = display.intern_atom
    clients = root.get_full_property(atom('_NET_CLIENT_LIST'), X.AnyPropertyType)
    result = []
    for identity in clients.value if clients is not None else ():
        window = display.create_resource_object('window', int(identity))
        try:
            title = window.get_full_property(atom('_NET_WM_NAME'), X.AnyPropertyType)
            value = title.value if title is not None else window.get_wm_name() or ''
            if isinstance(value, bytes):
                value = value.decode('utf-8', errors='replace')
            types = window.get_full_property(atom('_NET_WM_WINDOW_TYPE'), X.AnyPropertyType)
            normal = types is None or atom('_NET_WM_WINDOW_TYPE_NORMAL') in types.value
            result.append({'id': int(identity), 'title': str(value),
                'application': ' '.join(window.get_wm_class() or ()),
                'normal': normal, 'visible': window.get_attributes().map_state == X.IsViewable})
        except error.XError:
            continue  # A splash window can disappear during enumeration.
    return result


def wait(expected, observe, *, timeout=180, clock=time.monotonic, sleep=time.sleep):
    started = clock()
    stable = None
    observed = []
    while clock() - started < timeout:
        observed = observe()
        ready = next((window for window in observed if matches(window, expected)), None)
        if ready is not None and ready == stable:
            return {'expected': expected, 'window': ready, 'seconds': clock() - started}
        stable = ready
        sleep(.5)
    raise RuntimeError(f'desktop application did not become ready: {expected}; windows={observed}')


def main():
    if sys.argv[1] == '--wait':
        from Xlib.display import Display
        display = Display()
        try:
            print(json.dumps(wait(json.loads(sys.argv[2]), lambda: windows(display))))
        finally:
            display.close()
        return
    original = Path(__file__).with_name('osworld_setup.py')
    spec = importlib.util.spec_from_file_location('osworld_setup', original)
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    run_item = setup._run_item
    report = []

    def run_and_wait(item):
        run_item(item)
        expected = expectation(item)
        if expected is not None:
            process = subprocess.run(setup._ga_env() + [
                'python3', __file__, '--wait', json.dumps(expected)],
                capture_output=True, text=True, timeout=190, check=True)
            entry = json.loads(process.stdout)
            report.append(entry)
            print('[osworld readiness] ' + json.dumps(entry), flush=True)

    setup._run_item = run_and_wait
    setup.main()
    Path('/var/log/sandweave-osworld-readiness.json').write_text(json.dumps(report))


if __name__ == '__main__':
    main()
