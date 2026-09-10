# Files and setup

Read and write sandbox files through `env.files`:

```python
from sandweave import Sandbox

with Sandbox() as env:
    env.files.write_text("/workspace/main.py", "print(42)\n")
    print(env.files.read_text("/workspace/main.py"))
    print(env.run("python /workspace/main.py").stdout)
```

Guest paths refer to the sandbox. Host paths in `upload`, `download`, and `setup`
refer to the Python client's files, including when the sandbox runs remotely.

## Upload and download

Create a local `main.py` that writes a result:

```python title="main.py"
from pathlib import Path

Path("/workspace/result.txt").write_text("finished\n")
```

Then upload it, run it, and retrieve the output:

```python
from sandweave import Sandbox

with Sandbox() as env:
    env.files.upload("./main.py", "/workspace/main.py")
    env.run("python /workspace/main.py", check=True)
    env.files.download("/workspace/result.txt", "./result.txt")
```

Directory transfers include empty directories. Symlinks require explicit handling;
uploads and downloads do not follow them implicitly.

## Stream file contents

```python
with Sandbox() as env:
    with env.files.open("/workspace/notes.txt", "w") as stream:
        stream.write("first line\n")
        stream.write("second line\n")

    with env.files.open("/workspace/notes.txt") as stream:
        for line in stream:
            print(line, end="")
```

Binary modes such as `rb` and `wb` are supported. `read_bytes`, `write_bytes`,
`stat`, and `list` are also available.

## Run a setup script

Write an installation script on the client:

```bash title="install-tools.sh"
#!/bin/sh
set -eu
apt-get update
apt-get install -y jq
```

Use it while creating an environment:

```python
from sandweave import Sandbox

with Sandbox(setup="./install-tools.sh") as env:
    print(env.run("jq --version").stdout)
```

Or run it in an existing sandbox with `env.setup("./install-tools.sh")`. Setup
runs as guest root by default; it does not grant root privileges on the host.

If a script reads additional local files, declare them with
`env.setup("./install-tools.sh", inputs=["requirements.txt"])` or in a
[template](templates.md). Inputs must stay within the script's directory.

Use a [cache](snapshots.md) or `cache_key` to reuse the installed result.
