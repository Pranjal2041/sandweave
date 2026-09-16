"""Build files larger than the builder's RAM budget, and reclaim owned storage."""
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from sandweave import Sandbox, Memory
from sandweave.sandbox.targets import connect
from sandweave.sandbox.sandbox import definition
from sandweave.templates.build import build

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]


def test_large_image_and_failed_build_release_storage(tmp_path):
    context = tmp_path / 'context'
    context.mkdir()
    connection = connect(template=definition()['spec']['template'])
    try:
        root = Path(connection.call('ping')['workspace'])
        local = Path((root / 'runs/local-path.txt').read_text().strip())
        storage = local / 'service-data'
        before = set(storage.iterdir()) if storage.exists() else set()
        dockerfile = context / 'Dockerfile'
        # A second layer modifies a parent larger than the 4 GiB RAM budget.
        # Zeros keep the network archive small without making this a sparse file.
        dockerfile.write_text('FROM alpine:3.22 AS source\n'
            'RUN dd if=/dev/zero of=/large bs=1048576 count=4608 && chown 1234:5678 /large '
            '&& mknod /char-node c 10 175 && mknod /block-node b 8 0 && mknod /null-node c 1 3 '
            '&& mkdir /replaced && touch /replaced/old\n'
            'RUN chmod 640 /large && ln /large /hardlink && echo complete > /built '
            '&& rm -rf /replaced && mkdir /replaced && touch /replaced/new\n'
            # BuildKit's pinned fsutil clears S_IFSOCK unconditionally in
            # copyDevice(), changing block nodes into character nodes. Keep
            # those nodes in their original layer while exercising cross-stage
            # copying of the large file and an opaque replacement directory.
            'FROM source\nCOPY --from=source /large /large-copy\n'
            'COPY --from=source /replaced /copied\n')
        with ThreadPoolExecutor(1) as executor:
            building = executor.submit(build, context, timeout=600)
            # The parent reserves the importer before it has a sandbox record.
            # An unrelated launch must continue throughout image preparation.
            while not building.done():
                pending = [child for record in connection.call('list')
                           for child in record.get('image_imports', [])
                           if not (root / 'sandboxes' / (child + '.bin')).exists()]
                if pending:
                    with Sandbox(memory=Memory('256MiB', '128MiB')) as unrelated:
                        assert unrelated.run('printf responsive', check=True).stdout == 'responsive'
                    break
                time.sleep(.05)
            else:
                building.result()  # Surface any build failure first.
                pytest.fail('did not observe image import in progress')
            reference = building.result()
        try:
            with Sandbox(cache=reference) as env:
                assert env.run('stat -c "%s %u %g %a" /large', check=True).stdout == '4831838208 1234 5678 640\n'
                assert env.run('test /large -ef /hardlink && cat /built', check=True).stdout == 'complete\n'
                assert env.run('stat -c "%F %t %T" /char-node /block-node', check=True).stdout == (
                    'character special file a af\nblock special file 8 0\n')
                assert env.run('cat /null-node', check=True).stdout == ''
                env.run('test ! -e /replaced/old && test -e /replaced/new', check=True)
                env.run('test ! -e /copied/old && test -e /copied/new '
                        '&& test "$(stat -c %s /large-copy)" = 4831838208 '
                        '&& test "$(sha256sum /large | cut -d\' \' -f1)" = '
                        '"$(sha256sum /large-copy | cut -d\' \' -f1)"', timeout=120, check=True)
        finally:
            reference._connection.close()
        assert set(storage.iterdir()) == before
        dockerfile.write_text('FROM alpine:3.22\nRUN echo intentional-failure >&2; exit 7\n')
        with pytest.raises(RuntimeError, match='intentional-failure'):
            build(context)
        assert set(storage.iterdir()) == before
    finally:
        connection.close()
