"""Failures distinguish workload results from infrastructure outcomes."""


class SandboxError(RuntimeError):
    def __init__(self, message, *, operation_id=None, sandbox_id=None, phase=None):
        super().__init__(message)
        self.operation_id = operation_id
        self.sandbox_id = sandbox_id
        self.phase = phase


class CacheMiss(SandboxError):
    pass


class CacheConflict(SandboxError):
    pass


class IncompatibleSnapshot(SandboxError):
    pass


class UnsupportedFeature(SandboxError):
    pass


class ResourceUnavailable(SandboxError):
    pass


class OperationUnknown(SandboxError):
    pass


class SetupError(SandboxError):
    pass


class CommandError(SandboxError):
    def __init__(self, message, *, result=None, **kwargs):
        super().__init__(message, **kwargs)
        self.result = result
        self.returncode = None if result is None else result.returncode
        self.stdout = '' if result is None else result.stdout
        self.stderr = '' if result is None else result.stderr


class CommandTimeout(CommandError, TimeoutError):
    pass


class OutputLimitExceeded(CommandError):
    pass
