#!/usr/bin/env python3
"""Exercise real single-GPU autograd/AdamW training with a repeatable workload."""
import argparse
import json
import os
import time

started = time.perf_counter()
import torch

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--steps', type=int, default=120)
args = parser.parse_args()
if args.steps <= 10:
    parser.error('--steps must exceed the ten warmup steps')
torch.set_num_threads(4)
torch.manual_seed(42)
assert torch.cuda.is_available(), 'CUDA is unavailable'
assert torch.cuda.device_count() == 1, 'expected exactly one GPU'
torch.cuda.set_per_process_memory_fraction(0.25)
device = torch.device('cuda:0')
model = torch.nn.Sequential(
    torch.nn.Linear(1024, 4096), torch.nn.GELU(),
    torch.nn.Linear(4096, 4096), torch.nn.GELU(),
    torch.nn.Linear(4096, 128),
).to(device)
optimizer = torch.optim.AdamW(model.parameters(), lr=0.001)
x = torch.randn(512, 1024, device=device)
target = torch.tanh(x[:, :128])
initial_parameter = next(model.parameters()).detach().clone()
losses = []
print(json.dumps({'event': 'ready', 'torch': torch.__version__, 'cuda': torch.version.cuda,
                  'gpu': torch.cuda.get_device_name(), 'gpu_count': torch.cuda.device_count(),
                  'parameters': sum(p.numel() for p in model.parameters()),
                  'pid': os.getpid(), 'initialization_seconds': time.perf_counter() - started}), flush=True)

for step in range(args.steps):
    if step == 10:
        torch.cuda.synchronize()
        measured_start = time.perf_counter()
    optimizer.zero_grad(set_to_none=True)
    with torch.autocast('cuda', dtype=torch.bfloat16):
        prediction = model(x)
        loss = torch.nn.functional.mse_loss(prediction, target)
    loss.backward()
    optimizer.step()
    if step % 20 == 0 or step == args.steps - 1:
        value = loss.item()
        assert torch.isfinite(loss).item(), 'nonfinite loss'
        losses.append({'step': step, 'loss': value})
        print(json.dumps({'event': 'training', **losses[-1]}), flush=True)
torch.cuda.synchronize()
measured_seconds = time.perf_counter() - measured_start
assert losses[-1]['loss'] < losses[0]['loss'] * 0.25, 'training did not reduce loss'
assert not torch.equal(initial_parameter, next(model.parameters())), 'weights did not update'
assert all(torch.isfinite(p.grad).all().item() for p in model.parameters()), 'nonfinite gradients'

# Exercise device-to-host copies and ordinary framework model persistence too.
# This is not a whole-process/GPU-context snapshot.
checkpoint = '/tmp/general-vm-gpu-training-state.pt'
torch.save({'model': model.state_dict(), 'optimizer': optimizer.state_dict()}, checkpoint)
restored = torch.load(checkpoint, weights_only=True)
assert all(torch.equal(value, restored['model'][key]) for key, value in model.state_dict().items())
os.unlink(checkpoint)
print(json.dumps({'event': 'passed', 'steps': args.steps, 'warmup_steps': 10,
                  'measured_seconds': measured_seconds,
                  'steps_per_second': (args.steps - 10) / measured_seconds,
                  'first_loss': losses[0]['loss'], 'last_loss': losses[-1]['loss'],
                  'peak_gpu_allocated_bytes': torch.cuda.max_memory_allocated(),
                  'total_seconds': time.perf_counter() - started,
                  'model_optimizer_file_roundtrip': True}), flush=True)
