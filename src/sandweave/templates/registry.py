"""Read content-addressed Linux images from OCI Distribution registries."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from ..sandbox.workspace import locked

ACCEPT = ', '.join(('application/vnd.oci.image.index.v1+json',
                    'application/vnd.oci.image.manifest.v1+json',
                    'application/vnd.docker.distribution.manifest.list.v2+json',
                    'application/vnd.docker.distribution.manifest.v2+json'))


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise ValueError('image content must have a sha256 digest')
    return value.split(':')[1]


def reference(value):
    if not isinstance(value, str) or not value.startswith('docker://'):
        raise ValueError('image must be a docker:// image reference')
    name = value[9:]
    if not name or any(c.isspace() for c in name) or any(c in name for c in ('?', '#', '\\', '%')):
        raise ValueError('invalid Docker image reference')
    if '@' in name:
        name, version = name.rsplit('@', 1)
        digest(version)
        if ':' in name.rsplit('/', 1)[-1]:
            name = name.rsplit(':', 1)[0]
    else:
        last = name.rsplit('/', 1)[-1]
        name, version = name.rsplit(':', 1) if ':' in last else (name, 'latest')
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', version):
            raise ValueError('invalid image tag')
    first, separator, remainder = name.partition('/')
    if separator and ('.' in first or ':' in first or first == 'localhost'):
        host, repository = first, remainder
    else:
        host, repository = 'registry-1.docker.io', name
    if host in ('docker.io', 'index.docker.io'):
        host = 'registry-1.docker.io'
    if host == 'registry-1.docker.io' and '/' not in repository:
        repository = 'library/' + repository
    if not re.fullmatch(r'[a-z0-9.-]+(?::[0-9]+)?', host) or not all(
            re.fullmatch(r'[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*', p) for p in repository.split('/')):
        raise ValueError('invalid image registry or repository')
    return host, repository, version


class RegistryRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if urlsplit(url).scheme != 'https':
            raise ValueError('image registry redirected to a non-HTTPS URL')
        redirected = super().redirect_request(request, response, code, message, headers, url)
        if urlsplit(request.full_url).netloc != urlsplit(url).netloc:
            redirected.remove_header('Authorization')
        return redirected


class Registry:
    def __init__(self, image, directory):
        self.host, self.repository, self.version = reference(image)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.base = 'https://' + self.host + '/v2/' + self.repository
        self.token = None
        self.opener = build_opener(RegistryRedirect())

    def open(self, suffix, accept=None, *, authenticated=False):
        headers = {'User-Agent': 'sandweave', **({'Accept': accept} if accept else {})}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        try:
            return self.opener.open(Request(self.base + suffix, headers=headers), timeout=60)
        except HTTPError as error:
            if error.code != 401 or authenticated:
                raise
            challenge = error.headers.get('WWW-Authenticate', '')
            if not challenge.lower().startswith('bearer '):
                raise ValueError('image registry requires unsupported authentication') from error
            fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge[7:]))
            realm = fields.get('realm', '')
            parsed = urlsplit(realm)
            if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.fragment:
                raise ValueError('invalid image registry authentication endpoint') from error
            query = urlencode({'service': fields.get('service', self.host),
                               'scope': 'repository:' + self.repository + ':pull'})
            # Anonymous pull tokens only. Never send host credentials to a
            # registry-provided authentication endpoint or a blob redirect.
            with urlopen(realm + ('&' if parsed.query else '?') + query, timeout=60) as response:
                token = json.loads(response.read(1024*1024))
            self.token = token.get('token') or token.get('access_token')
            if not self.token:
                raise ValueError('registry did not issue an image pull token')
            return self.open(suffix, accept, authenticated=True)

    def manifest(self, version):
        with self.open('/manifests/' + version, ACCEPT) as response:
            body = response.read(16*1024*1024 + 1)
        if len(body) > 16*1024*1024:
            raise ValueError('image manifest is too large')
        identity = 'sha256:' + hashlib.sha256(body).hexdigest()
        if version.startswith('sha256:') and identity != version:
            raise ValueError('image manifest digest mismatch')
        return identity, json.loads(body)

    def resolve(self):
        identity, manifest = self.manifest(self.version)
        if 'manifests' in manifest:
            candidates = [d for d in manifest['manifests'] if
                          d.get('platform', {}).get('os') == 'linux' and
                          d['platform'].get('architecture') == 'amd64' and
                          d['platform'].get('variant', '') in ('', 'v1')]
            if not candidates:
                raise ValueError('image has no linux/amd64 manifest')
            selected = candidates[0]
            digest(selected['digest'])
            identity, manifest = self.manifest(selected['digest'])
        if manifest.get('schemaVersion') != 2 or 'config' not in manifest or 'layers' not in manifest:
            raise ValueError('image must use an OCI or Docker v2 manifest')
        if type(manifest['config'].get('size')) is not int or not 0 <= manifest['config']['size'] <= 16*1024*1024:
            raise ValueError('invalid image configuration size')
        config = json.loads(self.blob(manifest['config']).read_bytes())
        if config.get('os') != 'linux' or config.get('architecture') != 'amd64':
            raise ValueError('image must target linux/amd64')
        if config.get('rootfs', {}).get('type') != 'layers' or len(
                config['rootfs'].get('diff_ids', [])) != len(manifest['layers']):
            raise ValueError('image layer configuration does not match its manifest')
        for item in config['rootfs']['diff_ids']:
            digest(item)
        return {'digest': identity, 'platform': 'linux/amd64', 'manifest': manifest, 'config': config}

    def blob(self, descriptor):
        key = digest(descriptor['digest'])
        size = descriptor['size']
        if type(size) is not int or size < 0:
            raise ValueError('invalid image blob size')
        destination = self.directory / key
        with locked(self.directory / (key + '.lock')):
            if destination.is_file() and destination.stat().st_size == size:
                # Recheck cached downloads before incorporating them into a
                # new image. Prepared EROFS images have their own fast path.
                with destination.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() == key:
                        return destination
            fd, temporary = tempfile.mkstemp(prefix='.download-', dir=self.directory)
            try:
                total, checksum = 0, hashlib.sha256()
                with os.fdopen(fd, 'wb') as output, self.open('/blobs/' + descriptor['digest']) as response:
                    while block := response.read(1024*1024):
                        total += len(block)
                        if total > size:
                            raise ValueError('image blob exceeds declared size')
                        checksum.update(block)
                        output.write(block)
                if total != size or checksum.hexdigest() != key:
                    raise ValueError('image blob digest or size mismatch')
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return destination
