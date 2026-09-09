#!/usr/bin/env python3
"""Offline partial equirectangular exports from synchronized rectilinear eyes.

Requires explicit symmetric input FOVs. Unobserved directions remain black;
this cannot reconstruct a full panorama, depth, or new camera viewpoints.
Only reads local videos and invokes FFmpeg/ffprobe. Never starts a sandbox.
"""

import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess


def probe(path):
    return json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries',
        'stream=width,height,time_base,r_frame_rate,duration:'
        'frame=best_effort_timestamp',
        '-of', 'json', str(path),
    ]))


def timestamps(info):
    time_base = Fraction(info['streams'][0]['time_base'])
    return [int(frame['best_effort_timestamp']) * time_base
            for frame in info['frames']]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('left', type=Path)
    parser.add_argument('right', type=Path)
    parser.add_argument('--output', type=Path, required=True,
                        help='New directory; existing directories are refused')
    parser.add_argument('--hfov', type=float, required=True)
    parser.add_argument('--vfov', type=float, required=True)
    parser.add_argument('--fov-provenance', required=True,
                        help='Where the FOV values came from, including assumptions')
    parser.add_argument('--width', type=int, default=2048,
                        help='Per-eye 360-degree canvas width (height is half)')
    parser.add_argument('--threads', type=int, default=4)
    args = parser.parse_args()
    if not (0 < args.hfov < 180 and 0 < args.vfov < 180):
        parser.error('Rectilinear FOVs must be between 0 and 180 degrees')
    if args.width < 64 or args.width % 4 or args.threads < 1:
        parser.error('Width must be >=64 and divisible by 4; threads must be positive')

    sources = [args.left.resolve(strict=True), args.right.resolve(strict=True)]
    probes = [probe(path) for path in sources]
    times = [timestamps(info) for info in probes]
    if not times[0] or times[0] != times[1]:
        parser.error('Both eyes must contain frames with identical presentation timestamps')
    if times[0][0] != 0 or any(b <= a for a, b in zip(times[0], times[0][1:])):
        parser.error('Input timestamps must start at zero and strictly increase')
    args.output.mkdir(parents=True, exist_ok=False)

    # Explicit alpha + premultiplication is essential: the flat sampler can
    # clamp pixels outside its field of view. Mask those directions to black.
    projection = (
        'format=gbrap,'
        f'v360=input=flat:output=equirect:ih_fov={args.hfov}:'
        f'iv_fov={args.vfov}:w={args.width}:h={args.width // 2}:alpha_mask=1,'
        'premultiply=inplace=1,format=yuv420p,setsar=1'
    )
    stream = probes[0]['streams'][0]
    encoder = [
        '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
        '-threads', str(args.threads), '-pix_fmt', 'yuv420p',
        '-fps_mode', 'passthrough', '-enc_time_base', stream['time_base'],
        '-r', stream['r_frame_rate'], '-movflags', '+faststart',
    ]
    outputs = [args.output / f'{eye}-eye.mp4' for eye in ('left', 'right')]
    outputs.append(args.output / 'stereo-preview.mp4')
    commands = []
    for source, output in zip(sources, outputs):
        commands.append([
            'ffmpeg', '-nostdin', '-n', '-hide_banner', '-v', 'error',
            '-filter_threads', str(args.threads), '-i', str(source),
            '-vf', projection, *encoder, str(output),
        ])
    for command in commands:
        subprocess.run(command, check=True)
    stereo_command = [
        'ffmpeg', '-nostdin', '-n', '-hide_banner', '-v', 'error',
        '-filter_complex_threads', str(args.threads),
        '-i', str(outputs[0]), '-i', str(outputs[1]),
        '-filter_complex', '[0:v][1:v]hstack=inputs=2[v]', '-map', '[v]',
        *encoder, str(outputs[2]),
    ]
    commands.append(stereo_command)
    subprocess.run(stereo_command, check=True)

    exported = []
    for output in outputs:
        info = probe(output)
        if timestamps(info) != times[0]:
            raise RuntimeError(f'Output changed the presentation timestamps: {output}')
        exported.append({
            'path': str(output), 'sha256': digest(output),
            'bytes': output.stat().st_size, 'stream': info['streams'][0],
            'frames': len(info['frames']), 'timestamps_match_input': True,
        })
    manifest = {
        'projection': 'equirectangular', 'coverage': 'partial, forward view only',
        'canvas_degrees': [360, 180],
        'input_fov_degrees': [args.hfov, args.vfov],
        'fov_provenance': args.fov_provenance,
        'camera_model': 'symmetric rectilinear; head-relative orientation each frame',
        'unobserved_directions': 'black; no reconstruction or generated pixels',
        'limitations': [
            'Not a complete 180/360-degree recording',
            'No world stabilization, depth reconstruction, or new eye positions',
            'Original rendered corner masks remain in the captured region',
            'No physical-headset optical calibration or playback validation',
            'No spherical/stereo player metadata injected; select projection/layout manually',
            'Lossy resampling; canvas resolution is not captured scene resolution',
            'Audio is omitted',
        ],
        'sources': [{'path': str(path), 'sha256': digest(path)} for path in sources],
        'outputs': exported, 'commands': commands,
    }
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'output': str(args.output), 'frames_per_video': len(times[0]),
                      'timestamps_preserved': True}))


if __name__ == '__main__':
    main()
