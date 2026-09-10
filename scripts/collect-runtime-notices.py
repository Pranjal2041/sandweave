#!/usr/bin/env python3
"""Collect notices from the pinned engine, Go toolchain and declared Go modules."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import urllib.request
import zipfile


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def collect(archive, output):
    output.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        prefix = members[0].name.split('/')[0] + '/'
        module = source.extractfile(prefix + 'go.mod').read().decode()
        for member in members:
            if Path(member.name).name.upper() in ('LICENSE', 'NOTICE', 'COPYING') and member.isfile():
                relative = Path(member.name.removeprefix(prefix))
                assert not relative.is_absolute() and '..' not in relative.parts
                destination = output / 'gvisor' / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.extractfile(member).read())
    (output / 'go.mod').write_text(module)
    toolchain = re.search(r'^go ([0-9.]+)$', module, re.MULTILINE).group(1)
    go_url = 'https://raw.githubusercontent.com/golang/go/go' + toolchain + '/LICENSE'
    (output / 'Go-LICENSE').write_bytes(fetch(go_url))
    dependencies = re.findall(r'^\s+(\S+) (v\S+)', module, re.MULTILINE)
    def dependency(item):
        name, version = item
        escaped = ''.join('!' + c.lower() if c.isupper() else c for c in name)
        url = 'https://proxy.golang.org/' + escaped + '/@v/' + version + '.zip'
        data = fetch(url)
        count = 0
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            for entry in package.infolist():
                relative = Path(entry.filename.removeprefix(name + '@' + version + '/'))
                if entry.is_dir() or not re.match(r'^(licen[cs]e|notice|copying|copyright)([._-]|$)', relative.name, re.I):
                    continue
                assert not relative.is_absolute() and '..' not in relative.parts
                target = output / 'modules' / (escaped + '@' + version) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(package.read(entry))
                count += 1
        if count == 0:
            raise ValueError('No license file found for ' + name + '@' + version)
        return {'module': name, 'version': version, 'url': url,
                'archive_sha256': hashlib.sha256(data).hexdigest(), 'notices': count}
    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(dependency, dependencies))
    (output / 'sources.json').write_text(json.dumps({'go_license_url': go_url, 'modules': records}, indent=2) + '\n')
    (output / 'README.txt').write_text(
        'Notices for gVisor, Go and the modules declared by the pinned engine source.\n'
        'The module set also includes build tools and tests; inclusion here does not\n'
        'mean that every module is linked into every executable. Each notice applies\n'
        'to its respective upstream material. Sandweave modifications are in engine.patch.\n')
    print('Collected notices for Go ' + toolchain + ' and ' + str(len(records)) + ' modules')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-archive', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    collect(args.source_archive, args.output)
