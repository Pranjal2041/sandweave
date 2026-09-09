#!/usr/bin/env python3
"""Check allocation refusal and the distinction between NVML/device numbering."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gvisor_gpu


class AllocationTests(unittest.TestCase):
    def test_unallocated_device_rejected_before_device_access(self):
        with patch.dict(os.environ, {'SLURM_JOB_GPUS': '0,1', 'SLURM_STEP_GPUS': ''}):
            with self.assertRaisesRegex(ValueError, 'outside'):
                gvisor_gpu.allocated_device(2)

    def test_step_allocation_is_narrower_than_job(self):
        with patch.dict(os.environ, {'SLURM_JOB_GPUS': '0,1', 'SLURM_STEP_GPUS': '1'}):
            with self.assertRaisesRegex(ValueError, 'outside'):
                gvisor_gpu.allocated_device(0)

    def test_cpu_only_slurm_allocation_cannot_discover_host_devices(self):
        with patch.dict(os.environ, {'SLURM_JOB_ID': '123', 'SANDWEAVE_GPU_DEVICES': '0'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'outside'):
                gvisor_gpu.allocated_device(0)

    def test_local_visibility_uses_nvml_ordinal_and_uuid_not_minor(self):
        with tempfile.TemporaryDirectory() as directory:
            info = Path(directory) / 'information'
            info.write_text('Device Minor: 0\nGPU UUID: GPU-test\n')
            with patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': '3', 'NVIDIA_VISIBLE_DEVICES': 'GPU-test'}, clear=True), \
                 patch.object(Path, 'glob', return_value=[info]), \
                 patch.object(gvisor_gpu.subprocess, 'check_output', return_value='3, GPU-test\n'), \
                 patch.object(gvisor_gpu.os, 'open', return_value=42), patch.object(gvisor_gpu.os, 'close'):
                self.assertEqual(gvisor_gpu.eligible_devices(), [0])
                os.environ['CUDA_VISIBLE_DEVICES'] = '0'
                self.assertEqual(gvisor_gpu.eligible_devices(), [])

    def test_malformed_allocation_rejected(self):
        with patch.dict(os.environ, {'SLURM_JOB_GPUS': 'all', 'SLURM_STEP_GPUS': ''}):
            with self.assertRaisesRegex(ValueError, 'allocation'):
                gvisor_gpu.allocated_device(0)

    def test_device_minor_is_not_nvml_index(self):
        with tempfile.TemporaryDirectory() as directory:
            info = Path(directory) / 'information'
            info.write_text('Device Minor: 0\nGPU UUID: GPU-test\nBus Location: 0000:61:00.0\n')
            with patch.object(Path, 'glob', return_value=[info]), patch.object(
                    gvisor_gpu.subprocess, 'check_output', return_value='3, GPU-test\n'):
                result = gvisor_gpu.device_identity(0)
            self.assertEqual(result['device_minor'], 0)
            self.assertEqual(result['host_nvml_index'], 3)
            self.assertEqual(result['uuid'], 'GPU-test')

    def test_nvml_invisible_device_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            info = Path(directory) / 'information'
            info.write_text('Device Minor: 0\nGPU UUID: GPU-test\nBus Location: 0000:61:00.0\n')
            with patch.object(Path, 'glob', return_value=[info]), patch.object(
                    gvisor_gpu.subprocess, 'check_output', return_value='0, GPU-other\n'):
                with self.assertRaisesRegex(ValueError, 'not visible'):
                    gvisor_gpu.device_identity(0)


if __name__ == '__main__':
    unittest.main()
