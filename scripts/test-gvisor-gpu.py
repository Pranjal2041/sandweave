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
        devices = [{'device_minor': 0, 'uuid': 'GPU-test', 'host_nvml_index': 3, 'pci_bus_id': '0000:61:00.0'}]
        with patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': '3', 'NVIDIA_VISIBLE_DEVICES': 'GPU-test'}, clear=True), \
             patch.object(gvisor_gpu, 'gpu_inventory', return_value=('610.57.04', devices)), \
             patch.object(gvisor_gpu.os, 'open', return_value=42), patch.object(gvisor_gpu.os, 'close'):
            self.assertEqual(gvisor_gpu.eligible_devices(), [0])
            os.environ['CUDA_VISIBLE_DEVICES'] = '0'
            self.assertEqual(gvisor_gpu.eligible_devices(), [])

    def test_malformed_allocation_rejected(self):
        with patch.dict(os.environ, {'SLURM_JOB_GPUS': 'all', 'SLURM_STEP_GPUS': ''}):
            with self.assertRaisesRegex(ValueError, 'allocation'):
                gvisor_gpu.allocated_device(0)

    def test_device_minor_is_not_nvml_index(self):
        devices = [{'device_minor': 0, 'uuid': 'GPU-test', 'host_nvml_index': 3, 'pci_bus_id': '0000:61:00.0'}]
        with patch.object(gvisor_gpu, 'gpu_inventory', return_value=('610.57.04', devices)):
            result = gvisor_gpu.device_identity(0)
            self.assertEqual(result['device_minor'], 0)
            self.assertEqual(result['host_nvml_index'], 3)
            self.assertEqual(result['uuid'], 'GPU-test')

    def test_nvml_invisible_device_rejected(self):
        with patch.object(gvisor_gpu, 'gpu_inventory', return_value=('610.57.04', [])):
            with self.assertRaisesRegex(ValueError, 'cannot identify'):
                gvisor_gpu.device_identity(0)

    def test_nvml_inventory_without_procfs_driver_entries(self):
        xml = ('<nvidia_smi_log><driver_version>610.57.04</driver_version>'
               '<gpu id="00000000:82:00.0"><uuid>GPU-test</uuid><minor_number>2</minor_number>'
               '<pci><pci_bus_id>Unknown Error</pci_bus_id></pci></gpu></nvidia_smi_log>')
        with patch.object(gvisor_gpu.subprocess, 'check_output', return_value=xml), \
             patch.object(Path, 'read_text', side_effect=AssertionError('procfs must not be required')):
            version, devices = gvisor_gpu.gpu_inventory()
        self.assertEqual(version, '610.57.04')
        self.assertEqual(devices, [{'device_minor': 2, 'uuid': 'GPU-test', 'host_nvml_index': 0,
                                   'pci_bus_id': '00000000:82:00.0'}])

    def test_invalid_inventory_does_not_guess_device_minor(self):
        xml = ('<nvidia_smi_log><driver_version>610.57.04</driver_version>'
               '<gpu id="0000:82:00.0"><uuid>GPU-test</uuid><minor_number>N/A</minor_number>'
               '</gpu></nvidia_smi_log>')
        with patch.object(gvisor_gpu.subprocess, 'check_output', return_value=xml):
            with self.assertRaisesRegex(ValueError, 'device minor'):
                gvisor_gpu.gpu_inventory()


if __name__ == '__main__':
    unittest.main()
