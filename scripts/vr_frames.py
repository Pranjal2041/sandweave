"""Portable paired-eye frames and lossless recording, independent of the runtime."""
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time


@dataclass(frozen=True)
class Frame:
    sequence: int
    compositor_frame: int
    predicted_display_ns: int
    capture_begin_ns: int
    gpu_ready_ns: int
    published_ns: int
    host_observed_ns: int
    host_copy_ns: int
    width: int
    height: int
    rgba: bytes
    eye_count: int = 1  # Historical mono recordings remain readable.

    @property
    def eye_width(self):
        return self.width // self.eye_count

    def _eye_index(self, eye):
        if eye not in ('left', 'right'):
            raise ValueError('eye must be left or right')
        index = 0 if eye == 'left' else 1
        if index >= self.eye_count:
            raise ValueError('recording does not contain the right eye')
        return index

    def image(self, eye=None):
        from PIL import Image
        image = Image.frombytes('RGBX', (self.width, self.height), self.rgba)
        if eye is not None:
            x = self._eye_index(eye) * self.eye_width
            image = image.crop((x, 0, x + self.eye_width, self.height))
        return image.convert('RGB')

    def array(self, eye=None):
        import numpy as np
        pixels = np.frombuffer(self.rgba, dtype=np.uint8).reshape(self.height, self.width, 4)
        if eye is not None:
            x = self._eye_index(eye) * self.eye_width
            pixels = pixels[:, x:x+self.eye_width]
        return pixels[:, :, :3]

    @property
    def left(self):
        return self.array('left')

    @property
    def right(self):
        return self.array('right')

    def metadata(self):
        return {key: value for key, value in vars(self).items() if key != 'rgba'}


class FrameRecorder:
    """Bounded background lossless RGBX/Zstandard images with an indexed timeline."""
    def __init__(self, directory, *, capacity=32, workers=4):
        import zstandard  # Fail before starting worker threads if unavailable.
        if not (1 <= capacity <= 1024 and 1 <= workers <= 32):
            raise ValueError('invalid recorder capacity or worker count')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.queue = queue.Queue(capacity)
        self.saved = self.dropped = 0
        self.closed = False
        self.errors = []
        self.mutex = threading.Lock()
        self.index = (self.directory/'frames.jsonl').open('w')
        self.threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(workers)]
        for thread in self.threads:
            thread.start()

    def submit(self, frame):
        if self.closed:
            raise RuntimeError('recorder is closed')
        try:
            self.queue.put_nowait(frame)
        except queue.Full:
            self.dropped += 1

    def _worker(self):
        import zstandard
        compressor = zstandard.ZstdCompressor(level=1)
        while (frame := self.queue.get()) is not None:
            try:
                name = f'{frame.sequence:010d}.rgba.zst'
                begin = time.monotonic_ns()
                encoded = compressor.compress(frame.rgba)
                compressed = time.monotonic_ns()
                (self.directory/name).write_bytes(encoded)
                written = time.monotonic_ns()
                with self.mutex:
                    self.index.write(json.dumps({**frame.metadata(), 'file': name,
                                     'pixel_format': 'RGBX8', 'codec': 'zstd',
                                     'record_encode_ns': compressed-begin,
                                     'record_write_ns': written-compressed})+'\n')
                    self.saved += 1
            except Exception as error:
                with self.mutex:
                    if not self.errors:
                        self.errors.append(str(error))
            finally:
                self.queue.task_done()
        self.queue.task_done()

    def close(self):
        if self.closed:
            return
        self.closed = True
        for _ in self.threads:
            self.queue.put(None)
        for thread in self.threads:
            thread.join()
        self.index.close()
        if self.errors:
            raise RuntimeError('background recording failed: '+self.errors[0])

    def video(self, eye=None, *, ffmpeg='ffmpeg'):
        if not self.closed:
            raise RuntimeError('close the recorder before exporting video')
        return RecordedFrames(self.directory).video(eye, ffmpeg=ffmpeg)


class RecordedFrames:
    """Indexed, lossless images; observation and input clocks are retained."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.metadata = sorted((json.loads(line) for line in (self.directory/'frames.jsonl').read_text().splitlines()),
                               key=lambda frame: frame['sequence'])

    def __len__(self):
        return len(self.metadata)

    def __getitem__(self, index):
        import zstandard
        meta = self.metadata[index]
        width, height = meta['width'], meta['height']
        eye_count = meta.get('eye_count', 1)
        if eye_count not in (1, 2) or width % eye_count or not (16 <= width//eye_count <= 4096 and 16 <= height <= 4096):
            raise ValueError('invalid recorded image dimensions')
        if meta['codec'] != 'zstd' or meta['pixel_format'] != 'RGBX8':
            raise ValueError('unsupported recorded image encoding')
        if Path(meta['file']).name != meta['file']:
            raise ValueError('recorded image filename must be local')
        raw = zstandard.ZstdDecompressor().decompress((self.directory/meta['file']).read_bytes(),
                                                     max_output_size=width*height*4)
        if len(raw) != width*height*4:
            raise ValueError('recorded image byte length mismatch')
        return Frame(**{key: meta[key] for key in Frame.__dataclass_fields__ if key not in ('rgba', 'eye_count')},
                     rgba=raw, eye_count=eye_count)

    def video(self, eye=None, *, ffmpeg='ffmpeg'):
        if eye not in (None, 'left', 'right'):
            raise ValueError('eye must be left, right, or None for stereo')
        recording = self
        if len(recording) < 2:
            raise ValueError('video requires at least two recorded frames')
        target = (self.directory/('gameplay.mp4' if eye is None else eye+'-eye.mp4')).resolve()
        # PNG conversion and video encoding happen after live recording has
        # closed. Temporary previews are local and removed after encoding.
        with tempfile.TemporaryDirectory(prefix='vr-video-') as temporary:
            temporary = Path(temporary)
            def export(index):
                frame = recording[index]
                frame.image(eye).save(temporary/f'{index:010d}.png', compress_level=1)
            with ThreadPoolExecutor(max_workers=4) as pool:
                for _ in pool.map(export, range(len(recording))):
                    pass
            lines = ['ffconcat version 1.0']
            for i, meta in enumerate(recording.metadata):
                lines += [f"file '{i:010d}.png'", 'option framerate 1000']
                if i+1 < len(recording):
                    duration = (recording.metadata[i+1]['capture_begin_ns']-meta['capture_begin_ns'])/1e9
                    lines.append(f'duration {duration:.9f}')
            listing = temporary/'frames.ffconcat'
            listing.write_text('\n'.join(lines)+'\n')
            subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-f', 'concat', '-safe', '0',
                            '-i', str(listing), '-fps_mode', 'vfr', '-c:v', 'libx264',
                            '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p',
                            '-threads', '4', '-movflags', '+faststart', str(target)], check=True)
        return target
