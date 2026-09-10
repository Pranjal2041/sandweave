# Run commands

`env.run(...)` takes one command string and waits for it to finish:

```python
from sandweave import Sandbox

with Sandbox() as env:
    result = env.run("python -c 'print(2 + 2)'")
    print(result.stdout)
    print(result.stderr)
    print(result.returncode)
```

The command runs through `/bin/sh -c` **inside the sandbox**. Pipes, redirects,
environment expansion, and `&&` work as shell expressions there.

## Working directory and environment

```python
with Sandbox() as env:
    result = env.run("echo $MODE > result.txt && cat result.txt",
                     cwd="/workspace", env={"MODE": "evaluation"})
    print(result.stdout)
```

Each call starts a new process. A `cd` or `export` in one call does not change
the next call. The default working directory is `/workspace`.

## Exit codes and timeouts

```python
from sandweave import CommandError, Sandbox

with Sandbox() as env:
    result = env.run("python -c 'raise ValueError(42)'")
    assert result.returncode != 0
    print(result.stderr)

    try:
        env.run("false", check=True)
    except CommandError as error:
        print(error.result.returncode)
```

Use `timeout=5` to give a command a five-second execution deadline. A timeout
raises `CommandTimeout`; it is separate from a program's nonzero exit status.
`startup_timeout` on `Sandbox(...)` controls sandbox creation instead.

## Stream a process

```python
from sandweave import Sandbox

with Sandbox() as env:
    process = env.exec("python -u -c 'print(1); print(2)'")
    for line in process.stdout:
        print(line, end="")
    process.wait(check=True)
```

`exec` returns immediately with a `Process`. Use its `stdin`, `stdout`, `stderr`,
`poll()`, `wait()`, and `terminate()` methods. The SDK drains command output
within its configured output limit.

For a terminal, pass `pty=True`. Terminal stdout and stderr share one stream;
`process.resize(rows, cols)` changes its dimensions.

## Select a shell or literal arguments

```python
with Sandbox() as env:
    print(env.run("printf '%s\\n' {1..3}", shell="/bin/bash").stdout)
    print(env.run(argv=["python", "-c", "print(2 + 2)"]).stdout)
```

Use `argv` when you need literal arguments without a shell. Do not combine it
with a command string or the `shell` option.

## Output limits

Commands have a 64 MiB combined output limit by default. Override it with
`max_output_bytes`. Exceeding the limit terminates the command and raises
`OutputLimitExceeded` with partial output. Download large result files through
[file access](files.md) before terminating the sandbox.
