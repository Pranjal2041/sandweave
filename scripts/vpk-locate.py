#!/usr/bin/env python3
"""Locate selected resources in a VPK v1/v2 directory without extracting assets.

Format reference: ValveResourceFormat/ValvePak, Package.Read.cs.
This checks availability and ranges; it does not verify archive checksums.
"""
import argparse
import json
from pathlib import Path
import struct


def entries(index):
    with index.open('rb') as stream:
        magic, version, size = struct.unpack('<III', stream.read(12))
        if magic != 0x55AA1234 or version not in (1, 2) or size > 64*1024*1024:
            raise ValueError('unsupported VPK header or oversized directory')
        header_size = 12 if version == 1 else 28
        stream.seek(header_size)
        tree = stream.read(size)
    if len(tree) != size:
        raise ValueError('truncated VPK directory')
    position = 0

    def string():
        nonlocal position
        end = tree.index(b'\0', position)
        value = tree[position:end].decode('utf-8')
        position = end+1
        return value

    while extension := string():
        while directory := string():
            while name := string():
                crc, preload, archive, offset, length, terminator = struct.unpack_from('<IHHIIH', tree, position)
                position += 18
                if terminator != 0xffff or position+preload > len(tree):
                    raise ValueError('invalid VPK entry')
                position += preload
                resource = ('' if directory == ' ' else directory+'/')+name
                if extension != ' ':
                    resource += '.'+extension
                if archive == 0x7fff:
                    pack = index
                    offset += header_size+size
                else:
                    base = index.stem.removesuffix('_dir')
                    pack = index.with_name(f'{base}_{archive:03}.vpk')
                yield resource, pack, offset, length, preload, crc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('index', type=Path)
    parser.add_argument('resources', nargs='+')
    args = parser.parse_args()
    wanted = {name.casefold(): name for name in args.resources}
    found = {}
    for resource, pack, offset, length, preload, crc in entries(args.index):
        if resource.casefold() not in wanted:
            continue
        available = length == 0 or (pack.is_file() and pack.stat().st_size >= offset+length)
        found[resource.casefold()] = {'resource': resource, 'archive': str(pack),
                                     'offset': offset, 'length': length,
                                     'preload_bytes': preload, 'crc32': crc, 'available': available}
    print(json.dumps({'index': str(args.index), 'resources': [found.get(name.casefold(),
          {'resource': name, 'found': False}) for name in args.resources]}, indent=2))


if __name__ == '__main__':
    main()
