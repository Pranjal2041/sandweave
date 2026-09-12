"""Snapshot metadata checks and asynchronous verification of frozen artifacts."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import runtime_store


def write_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, indent=2)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def signature(path):
    stat = Path(path).stat()
    return {key: getattr(stat, key) for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}


def contained(root, name):
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('missing or invalid snapshot dependency: ' + str(path))
    return path


def identity(manifest):
    return manifest.get('snapshot_id') or hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def inspect(lab, snapshot, *, allow_failed=False):
    snapshot = Path(snapshot).resolve()
    manifest = json.loads((snapshot / 'snapshot-manifest.json').read_text())
    if manifest.get('format') not in (1, 2):
        raise ValueError('unsupported snapshot format')
    required = {'lab-spec.json', 'launch-settings.json', 'fixtures.tar'}
    if manifest.get('kind', 'live') == 'filesystem':
        required.add('rootfs-upper.tar')
        required.update(m['file'] for m in manifest['filesystem']['mounts'])
    elif manifest.get('kind', 'live') == 'live':
        required.update(('checkpoint.img', 'pages.img'))
    else:
        raise ValueError('unsupported snapshot kind')
    if not required <= manifest['files'].keys():
        raise ValueError('snapshot manifest is missing required files')
    for name, info in manifest['files'].items():
        if contained(snapshot, name).stat().st_size != info['size']:
            raise ValueError('snapshot size mismatch: ' + name)
    base = contained(lab, manifest['base_image']['path'])
    if base.stat().st_size != manifest['base_image']['size']:
        raise ValueError('snapshot base image size mismatch')
    settings = json.loads((snapshot / 'launch-settings.json').read_text())
    if settings['runtime'] != manifest['runtime']:
        raise ValueError('snapshot settings and runtime identity disagree')
    runtime_store.validate(lab, manifest['runtime'], verify=False)
    status_file = snapshot / 'verification.json'
    if status_file.exists():
        status = json.loads(status_file.read_text())
        failed = status['status'] == 'failed' or status.get('previous_failure')
        if status.get('snapshot_id') == identity(manifest) and failed and not allow_failed:
            raise ValueError('snapshot verification failed; run verify-snapshot.py after correcting the problem: ' +
                             (status.get('error') or status.get('previous_failure') or 'unknown error'))
    return manifest


def pending(snapshot, manifest):
    write_json(Path(snapshot) / 'verification.json', {
        'snapshot_id': identity(manifest), 'status': 'pending', 'queued_at': time.time(),
        'hostname': socket.gethostname(),
    })


def restore_path(local, snapshot, manifest):
    """Reuse this node's frozen capture when its recorded file identities match."""
    snapshot = Path(snapshot).resolve()
    reference = manifest.get('verification_source', {})
    if reference.get('hostname') != socket.gethostname():
        return snapshot
    source = Path(reference.get('path', '')).resolve()
    if not source.is_relative_to((Path(local) / 'gvisor/checkpoints').resolve()):
        return snapshot
    try:
        original = json.loads((source / 'snapshot-manifest.json').read_text())
        if identity(original) != identity(manifest):
            return snapshot
        for name in manifest['files']:
            if signature(contained(source, name)) != reference['files'][name]:
                return snapshot
    except (OSError, ValueError, KeyError):
        return snapshot
    return source


def start_verification(lab, snapshot):
    snapshot = Path(snapshot).resolve()
    with (snapshot / 'verification.log').open('ab') as output:
        try:
            child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('verify-snapshot.py')),
                                      '--lab', str(lab), str(snapshot)],
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        except OSError as error:
            status = json.loads((snapshot / 'verification.json').read_text())
            status.update(status='failed', error='Could not start verifier: ' + str(error), finished_at=time.time())
            write_json(snapshot / 'verification.json', status)
            raise
    return child


def verify(lab, snapshot, *, verified=None, reuse=False):
    """Compare new copies with their frozen source; recheck existing hashes later."""
    snapshot = Path(snapshot).resolve()
    with (snapshot / '.verification.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        started = time.perf_counter()
        manifest = json.loads((snapshot / 'snapshot-manifest.json').read_text())
        status = {'snapshot_id': identity(manifest), 'status': 'running', 'pid': os.getpid(),
                  'hostname': socket.gethostname(), 'started_at': time.time(), 'checked_files': []}
        status_file = snapshot / 'verification.json'
        if status_file.exists():
            previous = json.loads(status_file.read_text())
            if previous.get('snapshot_id') == identity(manifest):
                if reuse and previous['status'] == 'passed':
                    # Read receipts after acquiring the verification lock: an
                    # initial background verifier may have just finished.
                    verified = {**previous.get('verified_files', {}), **(verified or {})}
                if previous['status'] == 'failed' or previous.get('previous_failure'):
                    status['previous_failure'] = previous.get('error') or previous.get('previous_failure') or 'verification failed'
        write_json(snapshot / 'verification.json', status)
        cache = {}

        receipts = {}

        def digest(path, expected_stat=None, expected_sha256=None, *, immutable_base=False):
            before = signature(path)
            after = before
            content_identity = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
            if expected_stat is not None and before != expected_stat and not (
                    immutable_base and all(before[k] == expected_stat[k] for k in content_identity)):
                raise ValueError('captured source changed before verification: ' + str(path))
            key = tuple(before.values())
            receipt = (verified or {}).get(str(path))
            if receipt and receipt['signature'] == before and receipt['sha256'] == expected_sha256:
                cache[key] = receipt['sha256']
            if key not in cache:
                result = runtime_store.digest(path)
                after = signature(path)
                if after != before:
                    # An immutable base can acquire hard links before its first
                    # hash is pinned. Frozen checkpoint sources remain strict.
                    if ((not immutable_base and (expected_sha256 is None or result != expected_sha256)) or
                            any(after[k] != before[k] for k in content_identity)):
                        raise ValueError('file changed during verification: ' + str(path))
                cache[key] = result
                cache[tuple(after.values())] = result
            receipts[str(path)] = {'signature': after, 'sha256': cache[key]}
            return cache[key]

        try:
            inspect(lab, snapshot, allow_failed=True)
            reference = manifest.get('verification_source', {})

            def reference_path():
                if reference.get('hostname') != socket.gethostname():
                    raise ValueError('initial verification needs the frozen source on its original node')
                return Path(reference['path'])

            for name, info in manifest['files'].items():
                expected = info.get('sha256')
                if expected is None:
                    source = contained(reference_path(), name)
                    expected = digest(source, reference['files'][name])
                actual = digest(contained(snapshot, name), expected_sha256=expected)
                if actual != expected:
                    raise ValueError('snapshot digest mismatch: ' + name)
                info['sha256'] = actual
                status['checked_files'].append(name)
                write_json(snapshot / 'verification.json', status)
            base = manifest['base_image']
            expected = base.get('sha256')
            if expected is None:
                reference_path()
                expected = digest(Path(reference['base_path']), reference['base_stat'], immutable_base=True)
            if digest(contained(lab, base['path']), expected_sha256=expected) != expected:
                raise ValueError('base image digest mismatch')
            base['sha256'] = expected
            runtime = runtime_store.validate(lab, manifest['runtime'], verify=False)
            for name, expected in manifest['runtime']['sha256'].items():
                if digest(runtime / name, expected_sha256=expected) != expected:
                    raise ValueError('runtime digest mismatch: ' + name)
            # Publish hashes only after the complete comparison succeeds. Restore
            # readers see either complete metadata version through atomic rename.
            if manifest['format'] == 2:
                write_json(snapshot / 'snapshot-manifest.json', manifest)
            status.pop('previous_failure', None)
            status.update(status='passed', finished_at=time.time(), seconds=time.perf_counter() - started,
                          verified_files=receipts)
        except Exception as error:
            status.update(status='failed', error=str(error), finished_at=time.time(), seconds=time.perf_counter() - started)
        write_json(snapshot / 'verification.json', status)
        return status
