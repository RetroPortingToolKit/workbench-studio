import ctypes
import os
LIMIT_ENV = 'WORKBENCH_START_MEMORY_GB'
START_MEMORY_LIMIT_GB = 8.0
MB = 1024 * 1024
NO_HANDLE = 'no process handle'

class Exceeded(RuntimeError):
    pass

def limit_bytes(environ=None):
    text = (os.environ if environ is None else environ).get(LIMIT_ENV)
    if text is None or not str(text).strip():
        return int(START_MEMORY_LIMIT_GB * 1024 * MB)
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f'{LIMIT_ENV} must be a number of gigabytes, or 0 for no limit; it is {text!r}.') from None
    if value < 0:
        raise ValueError(f'{LIMIT_ENV} must not be negative; it is {text!r}.')
    return int(value * 1024 * MB) or None

def stop_text(record):
    peak = f" (peak {record['peak_process_mb']:,.0f} MB)" if record.get('peak_process_mb') else ''
    return f"a process of this start reached the memory limit of {record['limit_mb']:,} MB{peak}. A PS1 title has no use for that much: the title has a runaway allocation. Every process of the start was ended. This is a finding about the title, not a host failure (build); {LIMIT_ENV} changes the limit."

class Guard:

    def __init__(self, limit=None, reason=None):
        self.limit, self.reason = (limit, reason)

    def exceeded(self):
        return False

    def peaks(self):
        return (None, None)

    def record(self):
        if self.reason == NO_HANDLE:
            return None
        process, job = self.peaks()
        row = {'limit_mb': round(self.limit / MB) if self.limit else None}
        if process is not None:
            row['peak_process_mb'] = round(process / MB, 1)
            row['peak_job_mb'] = round(job / MB, 1)
        if self.reason:
            row['not_limited'] = self.reason
        if self.exceeded():
            row['exceeded'] = True
        return row

    def end(self):
        pass

    def close(self):
        pass
if os.name == 'nt':
    from ctypes import wintypes
    _kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    _EXTENDED_LIMIT, _ASSOCIATE_PORT = (9, 7)
    _LIMIT_PROCESS_MEMORY = 256
    _MSG_PROCESS_MEMORY_LIMIT, _MSG_JOB_MEMORY_LIMIT = (9, 10)
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    class _BasicLimit(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD), ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', _BasicLimit), ('IoInfo', _IoCounters), ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    class _AssociatePort(ctypes.Structure):
        _fields_ = [('CompletionKey', ctypes.c_void_p), ('CompletionPort', wintypes.HANDLE)]
    _kernel.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel.CreateIoCompletionPort.restype = wintypes.HANDLE
    _kernel.CreateIoCompletionPort.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_size_t, wintypes.DWORD]
    _kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    _kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel.GetQueuedCompletionStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD]
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    class JobGuard(Guard):

        def __init__(self, handle, limit):
            super().__init__(limit)
            self._job = self._port = None
            self._exceeded = False
            self._last = (None, None)
            job = _kernel.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), 'CreateJobObject failed')
            self._job = job
            try:
                info = _ExtendedLimit()
                info.BasicLimitInformation.LimitFlags = _LIMIT_PROCESS_MEMORY
                info.ProcessMemoryLimit = limit
                self._call(_kernel.SetInformationJobObject(job, _EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info)), 'set the memory limit')
                port = _kernel.CreateIoCompletionPort(_INVALID_HANDLE, None, 0, 1)
                if not port:
                    raise OSError(ctypes.get_last_error(), 'CreateIoCompletionPort failed')
                self._port = port
                link = _AssociatePort(None, port)
                self._call(_kernel.SetInformationJobObject(job, _ASSOCIATE_PORT, ctypes.byref(link), ctypes.sizeof(link)), 'watch the job')
                self._call(_kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(handle))), "put the start's process in the job")
            except OSError:
                self.close()
                raise

        @staticmethod
        def _call(ok, what):
            if not ok:
                raise OSError(ctypes.get_last_error(), f'could not {what}')

        def exceeded(self):
            if self._exceeded or self._port is None:
                return self._exceeded
            code, key, overlapped = (wintypes.DWORD(), ctypes.c_size_t(), ctypes.c_void_p())
            while _kernel.GetQueuedCompletionStatus(self._port, ctypes.byref(code), ctypes.byref(key), ctypes.byref(overlapped), 0):
                if code.value in (_MSG_PROCESS_MEMORY_LIMIT, _MSG_JOB_MEMORY_LIMIT):
                    self._exceeded = True
            return self._exceeded

        def peaks(self):
            if self._job is None:
                return self._last
            info = _ExtendedLimit()
            if _kernel.QueryInformationJobObject(self._job, _EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info), None):
                self._last = (info.PeakProcessMemoryUsed, info.PeakJobMemoryUsed)
            return self._last

        def end(self):
            if self._job is not None:
                _kernel.TerminateJobObject(self._job, 1)

        def close(self):
            self.exceeded()
            self.peaks()
            for name in ('_port', '_job'):
                handle = getattr(self, name)
                if handle is not None:
                    _kernel.CloseHandle(handle)
                    setattr(self, name, None)

def guard(child, environ=None):
    try:
        limit = limit_bytes(environ)
    except ValueError as error:
        return Guard(reason=str(error))
    if limit is None:
        return Guard(reason=f'switched off with {LIMIT_ENV}')
    if os.name != 'nt':
        return Guard(limit, reason='no job object on this platform; nothing limits the start')
    handle = getattr(child, '_handle', None)
    if handle is None:
        return Guard(limit, reason=NO_HANDLE)
    try:
        return JobGuard(handle, limit)
    except OSError as error:
        return Guard(limit, reason=f'the job object could not be set up: {error}')
