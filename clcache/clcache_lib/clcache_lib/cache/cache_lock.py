from ctypes import windll, wintypes
from pathlib import Path
# import datetime
from .ex import CacheLockException
# from ..utils import trace

class CacheLock:
    """Implements a lock for the object cache which
    can be used in 'with' statements."""

    INFINITE = 0xFFFFFFFF
    WAIT_ABANDONED_CODE = 0x00000080
    WAIT_TIMEOUT_CODE = 0x00000102

    def __init__(self, mutexName: str, timeoutMs: int):
        self._mutexName = "Local\\" + mutexName
        self._mutex = None
        self._timeoutMs = timeoutMs
        # self._t0 = None

    def create_mutex(self):
        self._mutex = windll.kernel32.CreateMutexW(
            None, wintypes.BOOL(False), self._mutexName
        )
        assert self._mutex

    def __enter__(self):
        self.acquire()

    def __exit__(self, typ, value, traceback):
        self.release()

    def __del__(self):
        if self._mutex:
            windll.kernel32.CloseHandle(self._mutex)

    def acquire(self):
        # trace(f"Acquiring lock {self._mutexName}...", 0)
        # self._t0 = datetime.datetime.now()
        if not self._mutex:
            self.create_mutex()
        result = windll.kernel32.WaitForSingleObject(
            self._mutex, wintypes.INT(self._timeoutMs)
        )
        if result not in [0, self.WAIT_ABANDONED_CODE]:
            if result == self.WAIT_TIMEOUT_CODE:
                errorString = f"Failed to acquire lock {self._mutexName} after {self._timeoutMs}ms."

            else:
                errorString = "Error! WaitForSingleObject returns {result}, last error {error}".format(
                    result=result, error=windll.kernel32.GetLastError()
                )
            # trace(errorString, 0)                
            raise CacheLockException(errorString)
        
        # elapsed = (datetime.datetime.now() - self._t0).total_seconds()
        # trace(f"Acquired lock {self._mutexName} after {elapsed:.3f} s", 0)

    def release(self):
        windll.kernel32.ReleaseMutex(self._mutex)
        # elapsed = (datetime.datetime.now() - self._t0).total_seconds()
        # trace(f"Released lock {self._mutexName} after {elapsed:.3f} s", 0)
        
    @staticmethod
    def for_path(path: Path):
        timeoutMs = 10 * 1000
        lockName = str(path).replace(":", "-").replace("\\", "-")
        return CacheLock(lockName, timeoutMs)
