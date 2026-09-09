"""Read the name of a selected GPU without polling NVML or enumerating access."""
from pathlib import Path


def describe(device, *, directory=Path('/proc/driver/nvidia/gpus')):
    """The recorded UUID is authoritative; never substitute a reused ordinal."""
    model = None
    try:
        for path in directory.glob('*/information'):
            fields = {key.strip(): value.strip() for key, value in
                      (line.split(':', 1) for line in path.read_text().splitlines() if ':' in line)}
            if fields.get('GPU UUID') == device['uuid']:
                model = fields.get('Model')
                break
    except OSError:
        pass
    return {'model': model, 'uuid': device['uuid'],
            'device': '/dev/nvidia' + str(device['device_minor'])}
