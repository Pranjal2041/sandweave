"""Bundle the qualified engine sources without maintaining a second source copy."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        root = Path(__file__).parent
        package = Path(self.build_lib) / 'sandweave'
        if package.resolve().is_relative_to((root / 'src').resolve()):
            raise ValueError('build output must be separate from package sources')
        # setuptools copies changed files but retains removed template files
        # from an earlier build. Publish only the current package sources.
        if package.exists():
            shutil.rmtree(package)
        super().run()
        destination = Path(self.build_lib) / 'sandweave/sandbox/runtimes/gvisor/_engine'
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for relative in (root / 'src/sandweave/engine-files.txt').read_text().splitlines():
            source = root / relative
            shutil.copy2(source, destination / source.name)


setup(cmdclass={'build_py': BuildPy})
