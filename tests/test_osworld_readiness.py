import pytest

from sandweave.templates.osworld.readiness import expectation, ready_window, wait


def test_launch_identification_handles_shell_assignments_and_non_gui_helpers():
    assert expectation({'type': 'launch', 'parameters': {
        'command': 'VLC_VERBOSE=-1 vlc --no-audio'}}) == {'application': 'vlc', 'document': ''}
    assert expectation({'type': 'launch', 'parameters': {
        'command': ['socat', 'tcp-listen:9222,fork', 'tcp:localhost:1337']}}) is None
    assert expectation({'type': 'launch', 'parameters': {
        'command': ['gimp', '/home/user/Desktop/a picture.png']}}) == {
            'application': 'gimp', 'document': 'a picture'}
    assert expectation({'type': 'open', 'parameters': {
        'path': '/home/user/Sales.xlsx'}}) == {'application': 'libreoffice', 'document': 'Sales'}


def test_readiness_ignores_splash_unmapped_other_application_and_wrong_document():
    wanted = {'application': 'gimp', 'document': 'picture'}
    correct = {'id': 1, 'title': 'picture (imported) – GIMP',
               'application': 'gimp.Gimp', 'normal': True, 'visible': True}
    invalid = [{**correct, 'normal': False}, {**correct, 'visible': False},
               {**correct, 'application': 'code.Code'}, {**correct, 'title': 'another image'}]
    observations = iter([[item] for item in invalid] + [[correct], [correct]])
    ticks = [0]
    def sleep(seconds):
        ticks[0] += seconds
    result = wait(wanted, lambda: next(observations), clock=lambda: ticks[0], sleep=sleep)
    assert result['window'] == correct
    assert result['seconds'] == 2.5


def test_missing_application_is_setup_failure_instead_of_an_empty_desktop():
    ticks = [0]
    def sleep(seconds):
        ticks[0] += seconds
    with pytest.raises(RuntimeError, match='did not become ready'):
        wait({'application': 'gimp', 'document': ''}, lambda: [], timeout=1,
             clock=lambda: ticks[0], sleep=sleep)


def test_original_application_dialog_is_exposed_without_answering_it():
    expected = {'application': 'gimp', 'document': 'dog_with_background'}
    parent = {'id': 1, 'title': 'GNU Image Manipulation Program',
              'application': 'gimp.Gimp', 'normal': True, 'visible': True}
    dialog = {'id': 2, 'title': 'Convert to RGB Working Space?',
              'application': 'gimp.Gimp', 'normal': False, 'visible': True,
              'dialog': True, 'parent': 1}
    assert ready_window([parent, dialog], expected) == dialog
    assert ready_window([parent, {**dialog, 'parent': 3}], expected) is None
    assert ready_window([parent, {**dialog, 'visible': False}], expected) is None
