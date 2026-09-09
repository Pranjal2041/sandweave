"""A deliberately small point-mass simulation, not a robot-physics engine."""
import json
import math

from sandweave.sandbox.asyncio import dualmethod


class Robot:
    def __init__(self, sandbox):
        self.sandbox = sandbox

    @dualmethod
    def reset(self, *, position=0):
        return self.sandbox._call('control', name='robotics', method='reset', parameters={'position': position})

    @dualmethod
    def step(self, acceleration):
        return self.sandbox._call('control', name='robotics', method='step', parameters={'acceleration': acceleration})


class Attached:
    def __init__(self, context, config, cold=False):
        self.context, self.dt = context, float(config.get('dt', .1))
        if not math.isfinite(self.dt) or self.dt <= 0:
            raise ValueError('simulation dt must be positive and finite')
        self.path = '/workspace/pointmass.json'
        self.script = '/workspace/pointmass-step.py'
        # Numerical state lives inside the guest and follows its filesystem/RAM
        # timeline. The provider never stores hidden episode state in the SDK.
        context.files.write_text(self.script, '''import json,sys
from pathlib import Path
path=Path(sys.argv[1]); dt=float(sys.argv[2]); a=float(sys.argv[3])
s=json.loads(path.read_text()); s['velocity']+=a*dt
s['position']+=s['velocity']*dt; s['steps']+=1
path.write_text(json.dumps(s)); print(json.dumps(s))
''')
        try:
            context.files.stat(self.path)
        except FileNotFoundError:
            self.call('reset', {'position': 0})

    def call(self, method, parameters):
        if method == 'reset':
            position = float(parameters.get('position', 0))
            if not math.isfinite(position):
                raise ValueError('position must be finite')
            state = {'position': position, 'velocity': 0, 'steps': 0}
            self.context.files.write_text(self.path, json.dumps(state))
            return state
        if method != 'step':
            raise ValueError('unknown point-mass operation')
        acceleration = float(parameters['acceleration'])
        if not math.isfinite(acceleration):
            raise ValueError('acceleration must be finite')
        result = self.context.run(argv=['python', self.script, self.path, str(self.dt), str(acceleration)])
        return json.loads(result['stdout'])

    def detach(self, reason):
        return False


class Provider:
    api_version = 1

    @staticmethod
    def descriptor(config):
        return {'version': 1, 'observation': 'position, velocity, steps', 'action': 'acceleration',
                'state': ['filesystem', 'memory'], 'step': 'one deterministic semi-implicit Euler step',
                'scope': 'toy point mass; no collision, articulated robot, camera or contact physics'}

    @staticmethod
    def bind(sandbox, config):
        return Robot(sandbox)

    @staticmethod
    def attach(context, config, cold=False):
        return Attached(context, config, cold)
