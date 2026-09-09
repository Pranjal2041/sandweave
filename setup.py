"""Bundle the qualified engine sources without maintaining a second source copy."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        destination = Path(self.build_lib) / 'sandweave/sandbox/runtimes/gvisor/_engine'
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for relative in (root / 'src/sandweave/engine-files.txt').read_text().splitlines():
            source = root / relative
            shutil.copy2(source, destination / source.name)


setup(cmdclass={'build_py': BuildPy})
