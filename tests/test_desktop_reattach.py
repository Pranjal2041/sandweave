"""A warm desktop reattach (after a pause or snapshot) waits only for a fast I/O frame."""
import types

from sandweave.templates.gnome import controls


class Client:
    def __init__(self, failures=0):
        self.failures, self.calls = failures, 0

    def screenshot(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError('fast I/O not ready yet')
        return {'size': [1920, 1080]}

    def close(self):
        pass


def context(client):
    def locked_down(*args, **kwargs):
        raise PermissionError('xprop: permission denied')  # the application locked guest tools for its user
    manager = types.SimpleNamespace(fast_io=lambda identity, backend: client)
    return types.SimpleNamespace(id='sw-test', runtime=types.SimpleNamespace(manager=manager),
                                 remaining=lambda limit: 5, run=locked_down)


def test_warm_reattach_needs_only_a_frame_not_guest_tools(monkeypatch):
    monkeypatch.setattr(controls.time, 'sleep', lambda seconds: None)
    client = Client(failures=2)
    controls.AttachedDesktop(context(client), {'wait_for_paint': False}, cold=False)
    assert client.calls == 3


def test_cold_attach_still_checks_the_window_manager(monkeypatch):
    monkeypatch.setattr(controls.time, 'sleep', lambda seconds: None)
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(controls.time, 'monotonic', lambda: next(clock))
    try:
        controls.AttachedDesktop(context(Client()), {'wait_for_paint': False}, cold=True)
        raise AssertionError('cold attach must not succeed without the window manager check')
    except PermissionError:
        pass
