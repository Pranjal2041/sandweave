"""Harbor task definitions and its original trial lifecycle on Sandweave."""
from pathlib import Path
from collections import Counter
import hashlib
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
            records = self.loop.call(self._load(source))
            self.paths = [path for path, _ in records]
            # Discovery reads metadata. Trial validates verifier inputs after
            # applying the caller's native Harbor configuration (including
            # verifier.disable and a separate verifier environment).
            definitions = [Task(task_dir=path, disable_verification=True) for path in self.paths]
            names = Counter(task.name for task in definitions)
            self.definitions, self.task_configs, tasks = {}, {}, []
            for task, (path, config) in zip(definitions, records, strict=True):
                identity = task.name
                if names[identity] > 1:
                    identity += '-' + hashlib.sha256(config.model_dump_json().encode()).hexdigest()[:16]
                if identity in self.definitions:
                    continue  # The exact same source may occur in two dataset selections.
                self.definitions[identity] = task
                self.task_configs[identity] = config
                tasks.append(TaskSpec(identity, task.instruction,
                    {'path': str(path), 'source': config.model_dump(mode='json')}))
            self.tasks = tuple(tasks)
        except BaseException:
            self.loop.close()
            raise

    async def _load(self, source):
        from harbor.models.job.config import DatasetConfig
        from harbor.models.trial.config import TaskConfig
        from harbor.tasks.client import TaskClient
        from ...sandbox.workspace import home
        directory = home() / 'benchmarks' / 'harbor' / 'tasks'
        if isinstance(source, (list, tuple)):
            records = []
            for item in source:
                records.extend(await self._load(item))
            if not records:
                raise ValueError('Harbor dataset contains no tasks')
            return records
        if isinstance(source, TaskConfig):
            config = source.model_copy(deep=True)
            config.download_dir = config.download_dir or directory
            result = (await TaskClient().download_tasks([config.get_task_id()],
                output_dir=config.download_dir, overwrite=config.overwrite)).results[0]
            return [(result.path, self._resolved(config, result))]
        if isinstance(source, DatasetConfig):
            dataset = source.model_copy(deep=True)
        elif isinstance(source, dict):
            dataset = DatasetConfig.model_validate(source)
        else:
            path = Path(source).expanduser()
            if path.exists():
                if (path / 'task.toml').is_file():
                    return [(path.resolve(), TaskConfig(path=path.resolve()))]
                dataset = DatasetConfig(path=path.resolve())
            elif isinstance(source, Path) or str(source).startswith(('/', '.', '~')):
                raise FileNotFoundError(path)
            elif str(source).startswith(('https://', 'ssh://', 'git@')):
                dataset = DatasetConfig(repo=str(source))
            else:
                name, separator, version = str(source).partition('@')
                dataset = DatasetConfig(name=name, **(
                    {('ref' if '/' in name else 'version'): version} if separator else {}))
        if dataset.download_dir is not None and 'download_dir' in dataset.model_fields_set:
            directory = dataset.download_dir
        else:
            dataset.download_dir = directory
        # Harbor owns discovery, registry selection, filtering and package
        # resolution. In particular, its package registry is distinct from
        # the legacy registry selected by RegistryClientFactory.create().
        configs = await dataset.get_task_configs(disable_verification=True)
        if not configs:
            raise ValueError('Harbor dataset contains no tasks')
        result = await TaskClient().download_tasks(
            [config.get_task_id() for config in configs],
            output_dir=directory, overwrite=dataset.overwrite)
        return [(download.path, self._resolved(config.model_copy(update={'download_dir': directory}), download))
                for config, download in zip(configs, result.results, strict=True)]

    @staticmethod
    def _resolved(config, result):
        config = config.model_copy(update={'overwrite': False})
        if config.is_git_task() and result.resolved_git_commit_id:
            config.git_commit_id = result.resolved_git_commit_id
        if config.is_package_task() and result.content_hash:
            config.ref = 'sha256:' + result.content_hash.removeprefix('sha256:')
        return config

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
