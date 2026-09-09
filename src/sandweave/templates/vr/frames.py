"""Load the shared portable frame codec bundled with the qualified engine sources."""
import importlib.util
import sys

from ...sandbox.workspace import engine_sources

_name = 'sandweave.templates.vr._frame_codec'
if _name not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_name, engine_sources() / 'vr_frames.py')
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _module
    _spec.loader.exec_module(_module)
else:
    _module = sys.modules[_name]

Frame = _module.Frame
FrameRecorder = _module.FrameRecorder
RecordedFrames = _module.RecordedFrames
