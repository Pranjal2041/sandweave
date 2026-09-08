"""Publish immutable, content-identified runtime builds for reproducible snapshots."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def from_directory(lab, path):
    """Select and verify an existing immutable build without publishing it."""
    lab = Path(lab).resolve()
    root = (lab / path).resolve()
    if not root.is_relative_to((lab / 'tools/runtime-builds').resolve()):
        raise ValueError('runtime must be in the immutable build store')
    descriptor = {'path': str(root.relative_to(lab)),
                  'sha256': json.loads((root / 'manifest.json').read_text())}
    validate(lab, descriptor, verify=True)
    return descriptor


def publish(lab, files):
    hashes = {name: digest(source) for name, source in files.items()}
    identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    root = lab / 'tools/runtime-builds'
    root.mkdir(exist_ok=True)
    target = root / identity
    if not target.exists():
        temporary = Path(tempfile.mkdtemp(prefix='.building-', dir=root))
        try:
            for name, source in files.items():
                destination = temporary / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                if digest(destination) != hashes[name]:
                    raise RuntimeError('runtime changed while being published: ' + name)
            (temporary / 'manifest.json').write_text(json.dumps(hashes, indent=2) + '\n')
            try:
                temporary.rename(target)
            except FileExistsError:
                pass
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {'path': str(target.relative_to(lab)), 'sha256': hashes}


def validate(lab, descriptor, *, verify=True):
    root = (lab / descriptor['path']).resolve()
    if not root.is_relative_to((lab / 'tools/runtime-builds').resolve()):
        raise ValueError('runtime must be in the immutable build store')
    if json.loads((root / 'manifest.json').read_text()) != descriptor['sha256']:
        raise ValueError('runtime manifest does not match the recorded build')
    for name, expected in descriptor['sha256'].items():
        artifact = (root / name).resolve()
        if not artifact.is_relative_to(root) or not artifact.is_file():
            raise ValueError('missing or invalid runtime file: ' + name)
        if verify and digest(artifact) != expected:
            raise ValueError('runtime digest mismatch: ' + name)
    return root
