"""Length-delimited JSON metadata and raw binary payloads; no pickle or base64."""
import json
import struct

MAX_BODY = 64 * 1024**2
MAX_HEADER = 4 * 1024**2


def encode(value, *, canonical=False):
    blobs, locations = [], []

    def walk(item, path):
        if isinstance(item, (bytes, bytearray, memoryview)):
            blob = bytes(item)
            blobs.append(blob)
            locations.append({'path': path, 'size': len(blob)})
            return None
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise TypeError('wire mapping keys must be strings')
            keys = sorted(item) if canonical else item
            return {key: walk(item[key], [*path, key]) for key in keys}
        if isinstance(item, (list, tuple)):
            return [walk(v, [*path, i]) for i, v in enumerate(item)]
        return item

    message = walk(value, [])
    header = json.dumps({'message': message, 'blobs': locations},
                        separators=(',', ':'), allow_nan=False).encode()
    if len(header) > MAX_HEADER:
        raise ValueError('wire metadata exceeds its limit')
    result = struct.pack('!Q', len(header)) + header + b''.join(blobs)
    if len(result) > MAX_BODY:
        raise ValueError('wire payload exceeds its limit; use streaming chunks')
    return result


def decode(data):
    if not 8 <= len(data) <= MAX_BODY:
        raise ValueError('invalid wire body length')
    length, = struct.unpack('!Q', data[:8])
    if length > MAX_HEADER or length + 8 > len(data):
        raise ValueError('invalid wire metadata length')
    header = json.loads(data[8:8+length])
    result, offset = header['message'], 8 + length
    seen = set()
    for blob in header['blobs']:
        path, size = blob['path'], blob['size']
        if type(size) is not int or size < 0 or offset + size > len(data):
            raise ValueError('invalid binary payload length')
        key = tuple(path)
        if key in seen:
            raise ValueError('duplicate binary payload location')
        seen.add(key)
        payload, offset = data[offset:offset+size], offset + size
        if not path:
            if result is not None:
                raise ValueError('invalid binary placeholder')
            result = payload
            continue
        parent = result
        for part in path[:-1]:
            parent = parent[part]
        if parent[path[-1]] is not None:
            raise ValueError('invalid binary placeholder')
        parent[path[-1]] = payload
    if offset != len(data):
        raise ValueError('trailing wire payload')
    return result
