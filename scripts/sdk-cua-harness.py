#!/usr/bin/env python3
"""Run the unchanged CUA catalog through an owned SDK desktop."""
import argparse
from pathlib import Path
import sys
import time

from sandweave import Sandbox, Slurm
from autoharness.backend import GenericProbeAdapter
from autoharness.driver import run_catalog
from cua_harness_adapter import GVisorXvncAdapter


class SDKDesktop(GVisorXvncAdapter):
    def __init__(self, target):
        self.target, self.env, self.client = target, None, None
        self.runner = 'fastio'

    def boot(self):
        self.env = Sandbox(template={'extends': 'gnome', 'capabilities': {
            'desktop': {'resolution': [1920, 1080]}}}, target=self.target)
        self.client = self.env.desktop
        self.env.desktop.keyboard.press('Escape')
        time.sleep(.5)
        result = self.env.run('python3 -c "import tkinter, Xlib"', check=False, user='root')
        if result.returncode:
            self.env.run('apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y python3-tk python3-xlib',
                         user='root', timeout=300)

    def exec(self, cmd, timeout=120):
        result = self.env.run(cmd, timeout=timeout, check=False, user='root')
        return result.returncode, result.stdout + result.stderr

    def copy_in(self, local_path, guest_path):
        self.env.files.upload(local_path, guest_path)

    def teardown(self):
        if self.env:
            try:
                self.env.terminate()
            finally:
                self.env.close()
                self.env = None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Catalog discovery follows the installed pinned harness CLI.
    from autoharness.contracts import load_catalog
    from autoharness.cli import _default_catalog
    target = Slurm.connect(args.job, cpus=12)
    backend = SDKDesktop(target)
    probe = GenericProbeAdapter(backend, name='sandweave-xvnc')
    try:
        report = run_catalog(probe, load_catalog(_default_catalog(None)), args.output)
        print(report['totals'], flush=True)
        return 0 if report['totals']['pass'] == 100 else 1
    finally:
        backend.teardown()
        connection = target.connection()
        try:
            connection.call('_shutdown_if_idle')
        finally:
            connection.close()


if __name__ == '__main__':
    sys.exit(main())
