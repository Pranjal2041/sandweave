"""Harbor task definitions and its original trial lifecycle on Sandweave."""
from pathlib import Path
import sys

from ..benchmark import TaskSpec


class Harbor:
    def __init__(self, *, source=None):
        if sys.version_info < (3, 12):
            raise RuntimeError('Harbor benchmarks require Python 3.12 or newer')
        try:
            from harbor.models.task.task import Task
        except ImportError as error:
            raise RuntimeError('Install sandweave[harbor] to run Harbor benchmarks') from error
        if source is None:
            raise ValueError('provide a Harbor dataset name or task directory as source')
        from .runner import Loop
        self.loop = Loop()
        try:
            self.paths = self.loop.call(self._load(source))
            self.definitions = {str(index): Task(task_dir=path) for index, path in enumerate(self.paths)}
            # Dataset names are not necessarily unique across registry namespaces.
            names = [task.name for task in self.definitions.values()]
            if len(set(names)) != len(names):
                raise ValueError('Harbor task names must be unique within a benchmark')
            self.definitions = {task.name: task for task in self.definitions.values()}
            self.tasks = tuple(TaskSpec(task.name, task.instruction, {'path': str(task.paths.task_dir)})
                               for task in self.definitions.values())
        except BaseException:
            self.loop.close()
            raise

    async def _load(self, source):
        from harbor.registry.client.factory import RegistryClientFactory
        from ...sandbox.workspace import home
        path = Path(source).expanduser()
        if path.exists():
            if (path / 'task.toml').is_file():
                return [path.resolve()]
            tasks = sorted(item.parent.resolve() for item in path.glob('*/task.toml'))
            if not tasks:
                raise ValueError('no Harbor tasks in ' + str(path))
            return tasks
        if isinstance(source, Path) or str(source).startswith(('/', '.', '~')):
            raise FileNotFoundError(path)
        directory = home() / 'benchmarks' / 'harbor' / 'tasks'
        items = await RegistryClientFactory.create().download_dataset(str(source), output_dir=directory)
        return [item.downloaded_path for item in items]

    def pool(self, *, capacity, preload, **options):
        from .runner import TaskPool
        self._pool = TaskPool(self, capacity=capacity, preload=preload, **options)
        return self._pool

    def setup(self, env, spec):
        pass  # Trial has completed setup before the pull returns.

    def evaluate(self, env, spec):
        return self.loop.call(self._pool.sessions[env.id].evaluate())

    def next_step(self, env, spec):
        return self.loop.call(self._pool.sessions[env.id].next_step())

    def close(self):
        self.loop.close()
