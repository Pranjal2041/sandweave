"""runtime="modal": one Modal sandbox per Sandweave sandbox, under Modal's own gVisor."""
from ...errors import UnsupportedFeature


def configure(recipe):
    """Adapt a resolved template for Modal before launch.

    Modal's gVisor cannot start systemd services, so templates that boot systemd
    run without it: VR templates keep their VR controls and run an X server,
    Monado and the game directly (no GNOME desktop). Desktop-only templates are
    rejected rather than silently started without their desktop.
    """
    capabilities = recipe['capabilities']
    if 'vr' in capabilities:
        capabilities.pop('desktop', None)
        recipe['runtime_options'].pop('init', None)
        recipe['runtime_options'].pop('init_command', None)
        return recipe
    if capabilities or recipe['runtime_options'].get('init', 'agent') != 'agent':
        raise UnsupportedFeature('runtime="modal" cannot boot systemd; desktop templates require runtime="gvisor"')
    raise UnsupportedFeature('runtime="modal" currently runs VR templates; use runtime="gvisor" for this template')
