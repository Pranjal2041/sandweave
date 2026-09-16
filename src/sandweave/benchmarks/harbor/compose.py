"""Resolve Harbor's Compose definitions with Docker's parser, without a daemon."""
import json
import asyncio
import hashlib
import math
import re
import shlex
from pathlib import Path
import subprocess
import tempfile
import tarfile
import time
from functools import partial

from harbor.environments.definition import should_use_prebuilt_docker_image
from harbor.environments.docker import COMPOSE_BUILD_PATH, COMPOSE_PREBUILT_PATH, write_env_compose_file, write_mounts_compose_file
from harbor.environments.docker.compose_env import ComposeInfraEnvVars, legacy_log_mount_env_vars, merge_compose_env

from ...sandbox.workspace import home
from ...sandbox.resources import CPU, Memory
from ..benchmark import drained, settled


def service_network(name, service):
    networks = {} if service.get('network_mode') == 'none' else service.get('networks', {'default': None})
    common = {name, service.get('hostname', name)}
    aliases = {network: sorted(common | set((settings or {}).get('aliases') or []))
               for network, settings in networks.items()}
    return {'networks': list(networks), 'aliases': sorted(common), 'network_aliases': aliases}


def duration(value, default):
    """Compose emits Go duration strings, including compound units."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value / 1e9
    units = {'h': 3600, 'm': 60, 's': 1, 'ms': .001, 'us': 1e-6, 'µs': 1e-6, 'ns': 1e-9}
    parts = re.findall(r'(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)', value)
    if ''.join(number + unit for number, unit in parts) != value:
        raise ValueError('Invalid Compose duration: ' + value)
    return sum(float(number) * units[unit] for number, unit in parts)


def file_mode(value):
    return int(value, 8) if isinstance(value, str) else value


def rendered_values(value):
    # `compose config` escapes dollar signs for feeding its output back into
    # Compose. We execute the resolved values directly, without that second
    # interpolation pass. This applies to commands, inline recipes and env.
    if isinstance(value, str):
        return value.replace('$$', '$')
    if isinstance(value, list):
        return [rendered_values(item) for item in value]
    if isinstance(value, dict):
        return {key: rendered_values(item) for key, item in value.items()}
    return value

COMPOSE_VERSION = 'v5.5.1'
COMPOSE_SHA256 = 'db1889184726840f75c4f9c001048430d4f25b3be3cb084d3ddd762bc0aed576'


def render(environment, force_build=False):
    from ...bootstrap import download
    binary = download('https://github.com/docker/compose/releases/download/' + COMPOSE_VERSION +
        '/docker-compose-linux-x86_64', home() / 'tools' / 'compose',
        'compose-' + COMPOSE_VERSION, sha256=COMPOSE_SHA256)
    binary.chmod(0o700)
    directory = environment.environment_dir.resolve()
    prebuilt = should_use_prebuilt_docker_image(directory,
        docker_image=environment.task_env_config.docker_image, force_build=force_build)
    paths = [COMPOSE_PREBUILT_PATH if prebuilt else COMPOSE_BUILD_PATH]
    if (directory / 'docker-compose.yaml').is_file():
        paths.append(directory / 'docker-compose.yaml')
    paths.extend(environment.extra_docker_compose_paths)
    infra = ComposeInfraEnvVars(main_image_name='sandweave-harbor', context_dir=str(directory),
        prebuilt_image_name=environment.task_env_config.docker_image if prebuilt else None,
        cpus=environment._effective_cpus,
        memory=f'{environment._effective_memory_mb}M' if environment._effective_memory_mb else None).to_env_dict()
    infra.update(legacy_log_mount_env_vars(environment._mounts, host_value='source'))
    import os
    values = merge_compose_env(base_env=os.environ, user_env=environment._startup_env(),
                               infra_env=infra, logger=environment.logger)
    with tempfile.TemporaryDirectory(prefix='sandweave-compose-') as temporary:
        paths.append(write_env_compose_file(Path(temporary) / 'environment.json', environment._startup_env()))
        if environment._mounts:
            paths.append(write_mounts_compose_file(Path(temporary) / 'mounts.json', environment._mounts))
        command = [str(binary), '--project-name', 'sandweave', '--project-directory', str(directory)]
        for path in paths:
            command += ['--file', str(path)]
        command += ['config', '--format', 'json']
        result = subprocess.run(command, env=values, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=60)
        if result.returncode:
            raise ValueError('Invalid Harbor Compose definition: ' + result.stderr)
        return rendered_values(json.loads(result.stdout))


class Project:
    def __init__(self, environment, config):
        self.environment, self.config = environment, config
        self.sandbox = None
        self.views, self.processes, self.snapshots = {}, {}, []
        self.volume_sources = {}
        self.started_at, self.healthy = {}, set()
        self.tmpfs = {}

    async def start(self, force_build=False):
        from ...sandbox.sandbox import Sandbox, definition
        from ...sandbox.services import view
        definitions = self.config['services']
        requests, volumes, mounts = {}, {}, {}
        target = (self.environment.session.pool.options.get('target') if self.environment.session
                  else self.environment.target)
        for name, service in definitions.items():
            if service.get('network_mode') not in (None, 'none'):
                raise ValueError('Native service groups do not yet implement network_mode=' + service['network_mode'])
            options = self.environment.options() if name == 'main' else {
                'template': {'name': 'harbor-service', 'command_shell': '/bin/sh'},
                'cpu': 1, 'memory': Memory(f'{self.environment._effective_memory_mb or 1024}MiB'),
                'network': self.environment.options()['network']}
            options = dict(options)
            options['target'] = target
            options['startup_timeout'] = self.environment.task_env_config.build_timeout_sec
            options['env'] = service.get('environment') or {}
            recipe = options['template'] = dict(options['template'])
            if service.get('working_dir'):
                recipe['workdir'] = service['working_dir']
            if service.get('user') is not None:
                recipe['user'] = service['user']
            limits = service.get('deploy', {}).get('resources', {}).get('limits', {})
            cpus = service.get('cpus') or limits.get('cpus')
            if cpus:
                options['cpu'] = CPU(max(1, math.ceil(float(cpus))), quota=float(cpus))
            memory = service.get('mem_limit') or limits.get('memory')
            if memory:
                options['memory'] = Memory(int(memory))
            if name == 'main' and self.environment.session:
                # Match single-service tasks: explicit benchmark resource
                # overrides take precedence over the task's resource defaults.
                options.update({key: value for key, value in self.environment.session.pool.options.items()
                                if key in ('cpu', 'memory', 'gpu', 'startup_timeout', 'keep_on_error')})
            if service.get('build'):
                from ...templates.build import build
                context = service['build']
                settings = {'template': recipe, 'dockerfile': context.get('dockerfile', 'Dockerfile'),
                            'dockerfile_inline': context.get('dockerfile_inline'),
                            'build_args': context.get('args'), 'stage': context.get('target'),
                            'force': force_build or context.get('no_cache', False),
                            'additional_contexts': context.get('additional_contexts'),
                            'secrets': {item.get('target', item['source']): self.config['secrets'][item['source']]
                                        for item in context.get('secrets', [])},
                            'network': context.get('network'), 'pull': context.get('pull', False),
                            'labels': context.get('labels'), 'platform': service.get('platform', 'linux/amd64'),
                            'timeout': self.environment.task_env_config.build_timeout_sec}
                if self.environment.session:
                    saved = await self.environment.session.pool.build(context['context'], settings)
                else:
                    saved = await drained(partial(build, context['context'], target=target, **settings))
                    self.snapshots.append(saved)
                options.pop('image', None)
                options.pop('template')
                options['cache'] = saved.id
            else:
                image = service['image']
                options['image'] = image if image.startswith('docker://') else 'docker://' + image
            requests[name] = await drained(partial(definition, **options))
            networks = service_network(name, service)['networks']
            if not networks or all(self.config.get('networks', {}).get(network, {}).get('internal') for network in networks):
                requests[name]['spec']['_service_internal'] = True
                requests[name]['spec']['resources']['network'] = {'mode': 'offline', 'allow_cidrs': [], 'proxy': None, 'policy': None}
            mounts[name] = []
            for mount in service.get('volumes', []):
                kind = mount['type']
                source = mount.get('source')
                if kind == 'tmpfs':
                    settings = mount.get('tmpfs', {})
                    options = []
                    if settings.get('size'):
                        options.append('size=' + str(settings['size']))
                    if 'mode' in settings:
                        options.append('mode=' + format(file_mode(settings['mode']), 'o'))
                    self.tmpfs.setdefault(name, []).append({'target': mount['target'], 'options': ','.join(options)})
                    continue
                if kind not in ('bind', 'volume'):
                    raise ValueError('Unsupported Compose volume type: ' + kind)
                key = hashlib.sha256((kind + ':' + (source or name + ':' + mount['target'])).encode()).hexdigest()[:32]
                if kind == 'bind':
                    path = Path(source)
                    if not path.exists():
                        if not mount.get('bind', {}).get('create_host_path', True):
                            raise FileNotFoundError(path)
                    else:
                        self.volume_sources[key] = path
                volumes[key] = {'file': kind == 'bind' and Path(source).is_file()}
                mounts[name].append({'name': key, 'target': mount['target'],
                                     'staging': '/.sandweave-runtime/volumes/' + key,
                                     'subpath': mount.get('volume', {}).get('subpath'),
                                     'copy': kind == 'volume' and not mount.get('volume', {}).get('nocopy', False),
                                     'read_only': mount.get('read_only', False)})
            for kind in ('configs', 'secrets'):
                for mount in service.get(kind, []):
                    source = mount['source']
                    value = self.config[kind][source]
                    key = hashlib.sha256((kind + ':' + source + ':' + name).encode()).hexdigest()[:32]
                    if value.get('file'):
                        self.volume_sources[key] = Path(value['file'])
                    elif 'content' in value:
                        self.volume_sources[key] = value['content'].encode()
                    elif value.get('environment'):
                        import os
                        self.volume_sources[key] = os.environ[value['environment']].encode()
                    else:
                        raise ValueError('Compose ' + kind + ' require a file, content, or environment source: ' + source)
                    volumes[key] = {'file': True}
                    destination = mount.get('target', source)
                    if not destination.startswith('/'):
                        destination = ('/run/secrets/' if kind == 'secrets' else '/') + destination
                    mounts[name].append({'name': key, 'target': destination,
                        'staging': '/.sandweave-runtime/volumes/' + key, 'copy': False,
                        'read_only': True, 'mode': file_mode(mount.get('mode', 0o444)),
                        **{field: mount[field] for field in ('uid', 'gid') if field in mount}})
        main = requests.pop('main')
        main['spec']['services'] = {name: {'request': request, 'volumes': mounts[name],
                                                **service_network(name, definitions[name])}
                                    for name, request in requests.items()}
        main['spec']['service_network'] = service_network('main', definitions['main'])
        main['spec']['service_volumes'] = volumes
        main['spec']['service_mounts'] = mounts['main']
        main['spec']['_guest_tools'] = True
        for definition in main['spec']['services'].values():
            definition['request']['spec']['_guest_tools'] = True
        # Even a one-service Compose project can own shared/bind volumes.
        main['spec']['service_group'] = True
        pending = asyncio.create_task(asyncio.to_thread(Sandbox._from_definition, main, target))
        try:
            self.sandbox = await asyncio.shield(pending)
        except asyncio.CancelledError:
            self.sandbox = await settled(pending)
            raise
        self.views['main'] = self.sandbox
        for name in requests:
            self.views[name] = await drained(view, self.sandbox, name)
        for key, source in self.volume_sources.items():
            with tempfile.TemporaryDirectory(prefix='sandweave-volume-') as temporary:
                path = source
                if isinstance(source, bytes):
                    path = Path(temporary) / 'content'
                    path.write_bytes(source)
                elif source.is_dir():
                    path = Path(temporary) / 'volume.tar'
                    def pack():
                        with tarfile.open(path, 'w') as archive:
                            archive.add(source, arcname='.')
                    await drained(pack)
                remote = '/tmp/.sandweave-volume-' + key
                await drained(self.sandbox.files.upload, path, remote)
                await drained(partial(self.sandbox._call, 'service_volume_import', name=key, path=remote))
                await self.sandbox.run.aio('rm -f -- ' + remote, cwd='/', user='root', check=True)
        started, visiting, initialized = set(), set(), set()
        async def start_service(name):
            if name in started:
                return
            if name in visiting:
                raise ValueError('Compose service dependency cycle')
            visiting.add(name)
            service = definitions[name]
            for other, dependency in service.get('depends_on', {}).items():
                await start_service(other)
                condition = dependency.get('condition', 'service_started')
                if condition == 'service_healthy':
                    await self.wait_healthy(other)
                elif condition == 'service_completed_successfully':
                    await self.processes[other].wait.aio(timeout=self.environment.task_env_config.build_timeout_sec)
                    result = await self.processes[other].result.aio()
                    if result.returncode:
                        raise RuntimeError('Compose dependency failed: ' + other)
            sandbox = self.views[name]
            settings = sandbox.spec.get('image', {}).get('settings', {})
            workdir = sandbox.spec.get('workdir') or service.get('working_dir') or settings.get('workdir')
            if workdir:
                await sandbox.run.aio('mkdir -p -- ' + shlex.quote(workdir), cwd='/', user='root', check=True)
            volume_mounts = []
            for mount in mounts[name]:
                volume_mounts.append({**mount, 'copy': mount['copy'] and mount['name'] not in initialized})
                initialized.add(mount['name'])
            prepared = await drained(partial(sandbox._call, 'service_setup', settings={
                'mounts': volume_mounts, 'hostname': service.get('hostname'),
                'extra_hosts': service.get('extra_hosts'), 'read_only': service.get('read_only', False),
                'ulimits': service.get('ulimits'), 'group_add': service.get('group_add', []),
                'restart': service.get('restart', 'no'), 'stop_signal': service.get('stop_signal', 'SIGTERM'),
                'tmpfs': [*self.tmpfs.get(name, []),
                          *([{'target': '/dev/shm', 'options': 'size=' + str(service['shm_size'])}]
                            if service.get('shm_size') is not None else []),
                          *[{'target': entry.split(':', 1)[0], 'options': entry.partition(':')[2]}
                            for entry in service.get('tmpfs', [])]]}))
            entrypoint = service.get('entrypoint')
            if entrypoint is None:
                entrypoint = settings.get('entrypoint') or []
            command = service.get('command')
            if command is None:
                command = [] if service.get('entrypoint') is not None else settings.get('cmd') or []
            arguments = [*entrypoint, *command]
            if not arguments:
                raise ValueError('Compose service has no command: ' + name)
            self.processes[name] = await sandbox.exec.aio(argv=[*(prepared.get('supervisor') or []), *arguments],
                                                        pty=service.get('tty', False))
            if not service.get('stdin_open') and not service.get('tty'):
                await self.processes[name].stdin.close.aio()
            await drained(partial(sandbox._call, 'service_setup',
                                  settings={'watch_process': self.processes[name].id}))
            self.started_at[name] = time.monotonic()
            started.add(name)
            visiting.remove(name)
        for name in definitions:
            await start_service(name)
        for name in definitions:
            await self.wait_healthy(name)
        return self.sandbox

    async def wait_healthy(self, name):
        service = self.config['services'][name]
        settings = self.views[name].spec.get('image', {}).get('settings', {})
        image_check = settings.get('healthcheck') or {}
        keys = {'Test': 'test', 'Interval': 'interval', 'Timeout': 'timeout', 'Retries': 'retries',
                'StartPeriod': 'start_period', 'StartInterval': 'start_interval'}
        check = {**{keys[key]: value for key, value in image_check.items() if key in keys},
                 **(service.get('healthcheck') or {})}
        test = check.get('test')
        if name in self.healthy or not test or check.get('disable') or test == ['NONE']:
            return
        deadline = time.monotonic() + self.environment.task_env_config.build_timeout_sec
        start_end = self.started_at[name] + duration(check.get('start_period'), 0)
        failures = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Compose service healthcheck timed out: ' + name)
            timeout = min(duration(check.get('timeout'), 30), remaining)
            if test[0] == 'CMD':
                arguments = test[1:]
            else:
                arguments = [*(settings.get('shell') or ['/bin/sh', '-c']), test[1]]
            process = await self.views[name].exec.aio(argv=arguments, timeout=timeout)
            await process.wait.aio()
            result = await process.result.aio()
            if result.returncode == 0:
                self.healthy.add(name)
                return
            starting = time.monotonic() < start_end
            if not starting:
                failures += 1
                if failures >= check.get('retries', 3):
                    raise RuntimeError('Compose service healthcheck failed: ' + name + ': ' + result.stderr)
            interval = duration(check.get('start_interval'), 5) if starting else duration(check.get('interval'), 30)
            await asyncio.sleep(min(interval, max(0, deadline - time.monotonic())))

    async def close(self):
        if self.sandbox is not None:
            try:
                try:
                    results = await asyncio.gather(*(self.environment.stop_service(name) for name in self.processes),
                                                   return_exceptions=True)
                    failures = [result for result in results if isinstance(result, Exception)]
                    if failures:
                        raise ExceptionGroup('service shutdown failed', failures)
                finally:
                    await self.sandbox.terminate.aio()
            finally:
                for view in self.views.values():
                    view._close_connection()
                self.sandbox._close_connection()
                self.views.clear()
                self.sandbox = None
        for snapshot in self.snapshots:
            snapshot._connection.close()
        self.snapshots.clear()
