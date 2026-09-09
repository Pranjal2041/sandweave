"""Resolve inspectable template recipes and explicitly declared input files."""
import copy
from dataclasses import dataclass
import hashlib
from importlib import resources
import json
from pathlib import Path
import re
import tomllib


def merge(parent, child):
    result = copy.deepcopy(parent)
    for key, value in child.items():
        if isinstance(value, dict) and value.get('enabled') is False:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass(frozen=True)
class Template:
    """A recipe supplied as a built-in name, TOML path or configuration mapping."""
    source: object = 'coding'

    def resolve(self):
        return resolve(self.source)


def _inputs(script, inputs, directory):
    script = Path(script)
    script = script if script.is_absolute() else directory / script
    if not script.is_file():
        raise FileNotFoundError(script)
    root = script.parent.resolve()
    files = {script.name: script.read_bytes()}
    for item in inputs:
        path = Path(item)
        path = path if path.is_absolute() else directory / path
        path = path.resolve()
        if not path.is_relative_to(root):
            raise ValueError('setup inputs must be within the script directory: ' + str(path))
        if not path.exists():
            raise FileNotFoundError(path)
        for source in ([path] if path.is_file() else sorted(path.rglob('*'))):
            if source.is_symlink():
                raise ValueError('declare regular setup files, not symlinks: ' + str(source))
            if source.is_file():
                files[str(source.relative_to(root))] = source.read_bytes()
    return {'script': script.name, 'files': files}


def setup_step(script, *, inputs=(), user='root', directory=None):
    payload = _inputs(script, inputs, Path(directory or '.').resolve())
    return {**payload, 'user': user}


def resolve(source='coding', _seen=()):
    if isinstance(source, Template):
        source = source.source
    if isinstance(source, dict):
        config = copy.deepcopy(source)
        directory, identity = Path.cwd(), '<object>'
    else:
        name = str(source)
        candidate = Path(name)
        if candidate.is_file():
            identity = str(candidate.resolve())
            directory = candidate.resolve().parent
            config = tomllib.loads(candidate.read_text())
        else:
            if not re.fullmatch(r'[a-z0-9_-]+(?:/[a-z0-9_-]+)*(?:@1)?', name):
                raise FileNotFoundError('template not found: ' + name)
            name = name.removesuffix('@1')
            candidate = resources.files('sandweave.templates').joinpath(name, 'template.toml')
            if not candidate.is_file():
                raise FileNotFoundError('template not found: ' + name)
            identity = 'builtin:' + name + '@1'
            directory = Path(str(candidate)).parent
            config = tomllib.loads(candidate.read_text())
    if identity in _seen:
        raise ValueError('template inheritance cycle: ' + ' -> '.join((*_seen, identity)))
    if config.get('schema_version', 1) != 1:
        raise ValueError('unsupported template schema version')
    parent = config.pop('extends', None)
    setup = config.pop('setup', None)
    if parent:
        parent_path = directory / parent
        parent = parent_path if parent_path.is_file() else parent
        base = resolve(parent, (*_seen, identity))
    else:
        base = {'schema_version': 1, 'resources': {}, 'services': {},
                'capabilities': {}, 'setup_steps': [], 'runtime_options': {},
                'user': 'root', 'command_shell': '/bin/sh'}
    result = merge(base, config)
    result['name'] = config.get('name', identity)
    if setup:
        if not isinstance(setup, dict) or 'script' not in setup:
            raise ValueError('setup must declare a script')
        result['setup_steps'] = [*base['setup_steps'], setup_step(
            setup['script'], inputs=setup.get('inputs', ()),
            user=setup.get('user', 'root'), directory=directory)]
    return result


def fingerprint(recipe):
    def encode(value):
        if isinstance(value, bytes):
            return {'sha256': hashlib.sha256(value).hexdigest(), 'size': len(value)}
        raise TypeError(type(value).__name__)
    return hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(',', ':'),
                                     default=encode, allow_nan=False).encode()).hexdigest()
