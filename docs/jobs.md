# Jobs

A job submits a command to a cluster and retains its attempts and result:

```python
from sandweave import Job

job = Job.submit("python -c 'print(2 + 2)'", target="lab", detached=True)
print(job.id)
result = job.result()
print(result.stdout)
```

`detached=True` lets it outlive the submitting Python process. Replace `lab`
with the printed controller address when using a remote connection.

## Upload a program

Create `evaluate.py` locally, then submit its bytes with the job:

```python
from sandweave import Job

job = Job.submit(
    "python /workspace/evaluate.py",
    files={"evaluate.py": "./evaluate.py"},
    target="lab",
    detached=True,
    timeout=60,
)
print(job.id)
```

Files are captured at submission. The local file does not need to remain
available after the request is accepted.

## Reconnect and inspect output

```python
from sandweave import Job

job = Job.connect("JOB_ID", target="lab")
result = job.result()
print(result.stdout)
print(result.stderr)
print(result.returncode)
job.close()
```

Replace `JOB_ID` with the saved ID. Nonzero program exits return results by
default. Use `check=True` to raise for them. Timeouts and output limits still
raise with a partial result.

`job.wait(timeout=30)` bounds the client's wait; it does not cancel the job.
The submission's `timeout` limits execution. Use `job.cancel()` to cancel it.

## Batch items and retries

Pass `items=[...]` to run a batch. Each item is available to the command as JSON
in `SANDWEAVE_ITEM`. Results preserve item order. The environment also contains
`SANDWEAVE_TASK_ID` and `SANDWEAVE_ATTEMPT`.

Retries are opt-in. `retries=2, retry_codes=[75]` permits two retries for that
exit code. `retry_infrastructure=True` additionally permits retries after a
confirmed infrastructure failure. A retry may repeat external effects; make
the submitted program safe to repeat before enabling them.

`every=60` schedules another attempt 60 seconds after the previous attempt ends,
until the job is cancelled.

## Stored output

Jobs retain at most 4 MiB of command output by default, configurable up to
16 MiB with `max_output_bytes`. Write larger artifacts to files and manage their
storage separately.

The [dashboard](dashboard.md) shows jobs, attempts, stdout, stderr, and exit
status. Controller restart retains recorded job state; it does not provide
automatic failover to another controller machine.
