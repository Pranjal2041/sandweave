#!/usr/bin/env python3
"""Measure synchronous CUDA transfers separately from GPU-resident computation."""
import json
import statistics
import time

import torch


torch.set_num_threads(1)
torch.cuda.init()
results = []
for mib in (8, 32):
    for pinned in (False, True):
        host = torch.ones(mib * 1024**2 // 4, dtype=torch.float32,
                          pin_memory=pinned)
        device = torch.empty_like(host, device='cuda')
        for direction in ('host_to_device', 'device_to_host'):
            if direction == 'device_to_host':
                host.zero_()
            src, dst = (host, device) if direction == 'host_to_device' else (device, host)
            elapsed = []
            for iteration in range(13):
                torch.cuda.synchronize()
                start = time.perf_counter()
                dst.copy_(src, non_blocking=True)
                torch.cuda.synchronize()
                if iteration >= 3:
                    elapsed.append(time.perf_counter() - start)
            seconds = statistics.median(elapsed)
            results.append({'mib': mib, 'pinned': pinned, 'direction': direction,
                            'median_ms': 1000 * seconds,
                            'gib_per_second': mib / 1024 / seconds,
                            'samples_ms': [1000 * value for value in elapsed]})
        assert torch.all(host == 1).item()
        del host, device
print(json.dumps({'torch': torch.__version__, 'gpu': torch.cuda.get_device_name(),
                  'results': results}, indent=2))
