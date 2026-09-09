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
        destination.mkdir(parents=True, exist_ok=True)
        for source in (root / 'scripts').iterdir():
            if source.suffix in ('.py', '.sh', '.c') and not source.name.startswith('test-'):
                shutil.copy2(source, destination / source.name)


setup(cmdclass={'build_py': BuildPy})
