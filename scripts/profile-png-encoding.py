import io
from pathlib import Path
import statistics
import sys
import time
import zlib
from PIL import Image, ImageChops

image = Image.open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/sw-fast-capture-20260920/results/capture_png-00.png').convert('RGB')
for filter_name in ('none', 'sub', 'up'):
    for strategy in (zlib.Z_RLE, zlib.Z_DEFAULT_STRATEGY):
        rows = []
        for _ in range(10):
            begin = time.perf_counter_ns()
            if filter_name != 'none':
                previous = Image.new('RGB', image.size)
                previous.paste(image, (1, 0) if filter_name == 'sub' else (0, 1))
                data = ImageChops.subtract_modulo(image, previous).tobytes()
            else:
                data = image.tobytes()
            stride = image.width * 3
            scan = bytearray((stride + 1) * image.height)
            for row in range(image.height):
                offset = row * (stride + 1)
                scan[offset] = {'none':0, 'sub':1, 'up':2}[filter_name]
                scan[offset + 1:offset + stride + 1] = data[row * stride:(row + 1) * stride]
            compressor = zlib.compressobj(1, zlib.DEFLATED, 15, 8, strategy)
            compressed = compressor.compress(scan) + compressor.flush()
            rows.append((time.perf_counter_ns() - begin) / 1e6)
        print(filter_name, strategy, round(statistics.mean(rows),3), len(compressed), flush=True)
