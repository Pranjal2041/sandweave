"""Bundle the qualified engine sources without maintaining a second source copy."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.egg_info import egg_info


def engine_inputs():
    return (Path(__file__).parent / 'src/sandweave/engine-files.txt').read_text().splitlines()


class EggInfo(egg_info):
    def find_sources(self):
        super().find_sources()
        # The source archive needs exactly the same engine inputs as the wheel,
        # without including unrelated experiments from the lab directories.
        self.filelist.extend(engine_inputs())
        self.filelist.sort()
        self.filelist.remove_duplicates()
        self.write_file('manifest file', str(Path(self.egg_info) / 'SOURCES.txt'),
                        '\n'.join(self.filelist.files) + '\n')


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
        for relative in engine_inputs():
            source = root / relative
            shutil.copy2(source, destination / source.name)


setup(cmdclass={'build_py': BuildPy, 'egg_info': EggInfo})
