"""ZRLE decoder for the lab's RGB32 VNC benchmark (RFC 6143, 7.7.5-6).

Packed palette rows are byte-aligned, including partial-width edge tiles.
The installed vncdotool decoder incorrectly treats these as one packed stream.
"""
import numpy as np


def decode(data, width, height):
    offset = 0

    def take(n):
        nonlocal offset
        if offset + n > len(data):
            raise ValueError('Truncated ZRLE rectangle')
        value = data[offset:offset+n]
        offset += n
        return value

    def run_length():
        length = 1
        while True:
            extra = take(1)[0]
            length += extra
            if extra != 255:
                return length

    for y in range(0, height, 64):
        for x in range(0, width, 64):
            w, h = min(64, width-x), min(64, height-y)
            count = w*h
            mode = take(1)[0]
            colors = mode & 127
            if mode in (127, 129) or 17 <= mode <= 126:
                raise ValueError('Reserved ZRLE subencoding')
            palette = np.frombuffer(take(colors*3), dtype=np.uint8).reshape(colors, 3)
            pixels = np.empty((count, 4), dtype=np.uint8)
            pixels[:, 3] = 255
            if mode == 0:
                pixels[:, :3] = np.frombuffer(take(count*3), dtype=np.uint8).reshape(count, 3)
            elif mode == 1:
                pixels[:, :3] = palette[0]
            elif mode < 128:
                bits = 1 if colors == 2 else 2 if colors <= 4 else 4
                row_bytes = (w*bits+7)//8
                packed = np.frombuffer(take(row_bytes*h), dtype=np.uint8).reshape(h, row_bytes)
                shifts = np.arange(8-bits, -1, -bits)
                indices = ((packed[:, :, None] >> shifts) & ((1 << bits)-1)).reshape(h, -1)[:, :w]
                if np.any(indices >= colors):
                    raise ValueError('Invalid ZRLE palette index')
                pixels[:, :3] = palette[indices.reshape(-1)]
            else:
                position = 0
                while position < count:
                    if colors == 0:
                        color = np.frombuffer(take(3), dtype=np.uint8)
                        length = run_length()
                    else:
                        index = take(1)[0]
                        if (index & 127) >= colors:
                            raise ValueError('Invalid ZRLE palette index')
                        color = palette[index & 127]
                        length = run_length() if index & 128 else 1
                    if position + length > count:
                        raise ValueError('ZRLE run exceeds tile')
                    pixels[position:position+length, :3] = color
                    position += length
            yield x, y, w, h, pixels.tobytes()
    if offset != len(data):
        raise ValueError('Trailing ZRLE data')


def handle(self, block, x, y, width, height):
    if self.bypp != 4:
        raise ValueError('The lab decoder requires the benchmark RGB32 format')
    for tx, ty, w, h, pixels in decode(self._zlib_stream.decompress(block), width, height):
        self.updateRectangle(x+tx, y+ty, w, h, pixels)
    self._doConnection()
