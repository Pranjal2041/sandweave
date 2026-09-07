#!/usr/bin/env python3
"""Verify the desaturated five-second Resolve acceptance export."""
import array
import json
import math
import subprocess
import sys

path = sys.argv[1]
info = json.loads(subprocess.check_output([
    'ffprobe', '-v', 'error', '-count_frames', '-show_streams', '-of', 'json', path,
]))
video = next(s for s in info['streams'] if s['codec_type'] == 'video')
audio = next(s for s in info['streams'] if s['codec_type'] == 'audio')
assert (video['width'], video['height'], int(video['nb_read_frames'])) == (1920, 1080, 120)
assert float(video['duration']) == 5 and float(audio['duration']) == 5

frames = []
for second in (0, 2, 4):
    frames.append(subprocess.check_output([
        'ffmpeg', '-v', 'error', '-ss', str(second), '-i', path, '-frames:v', '1',
        '-vf', 'scale=160:90', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-',
    ]))
assert all(len(frame) == 160 * 90 * 3 for frame in frames)
spread = sum(max(f[i:i+3]) - min(f[i:i+3]) for f in frames
             for i in range(0, len(f), 3)) / (len(frames) * 160 * 90)
assert spread < 2, f'Export is not desaturated: mean RGB spread {spread}'
assert len(set(frames)) == 3, 'Exported frames do not change'

pcm = array.array('f', subprocess.check_output([
    'ffmpeg', '-v', 'error', '-i', path, '-map', '0:a:0', '-ac', '1',
    '-ar', '48000', '-f', 'f32le', '-',
]))
if sys.byteorder != 'little':
    pcm.byteswap()
assert len(pcm) == 5 * 48000
rms = math.sqrt(sum(x*x for x in pcm) / len(pcm))
crossings = sum(a <= 0 < b for a, b in zip(pcm, pcm[1:]))
frequency = crossings / 5
assert rms > 0.01 and abs(frequency - 440) < 1, (rms, frequency)
print(json.dumps(dict(passed=True, video_codec=video['codec_name'],
                     video_frames=120, duration_seconds=5,
                     resolution=[1920, 1080], mean_rgb_spread=spread,
                     distinct_sampled_frames=3, audio_codec=audio['codec_name'],
                     audio_rms=rms, audio_frequency_hz=frequency), indent=2))
