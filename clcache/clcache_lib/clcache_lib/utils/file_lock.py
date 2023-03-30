import datetime
from ctypes import windll, wintypes
from pathlib import Path


class FileLockException(Exception):
    pass


class FileLock:
    """Implements a lock for the object cache which
    can be used in 'with' statements."""

    INFINITE = 0xFFFFFFFF
    WAIT_ABANDONED_CODE = 0x00000080
    WAIT_TIMEOUT_CODE = 0x00000102

    def __init__(self, mutex_name: str, timeout_ms: int):
        self._mutex_name = "Local\\" + mutex_name
        self._mutex = None
        self._timeout_ms = timeout_ms
        self._t0 = None
        self._acquired = False

    def create_mutex(self):
        self._mutex = windll.kernel32.CreateMutexW(
            None, wintypes.BOOL(False), self._mutex_name
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
        t0 = datetime.datetime.now()
        if not self._mutex:
            self.create_mutex()
        result = windll.kernel32.WaitForSingleObject(
            self._mutex, wintypes.INT(self._timeout_ms)
        )
        if result not in [0, self.WAIT_ABANDONED_CODE]:
            if result == self.WAIT_TIMEOUT_CODE:
                error_string = f"Failed to acquire lock {self._mutex_name} after {self._timeout_ms}ms."

            else:
                error_string = "Error! WaitForSingleObject returns {result}, last error {error}".format(
                    result=result, error=windll.kernel32.GetLastError()
                )
            from ..utils.logging import LogLevel, log
            log(error_string, LogLevel.ERROR)
            raise FileLockException(error_string)

        self._acquired = True
        self._t0 = datetime.datetime.now()
        
        elapsed = (self._t0  - t0).total_seconds()
        if elapsed > 2:
            from ..utils.logging import LogLevel, log
            log(
                f"Waited for lock {self._mutex_name} during {elapsed:.1f} s",
                LogLevel.TRACE,
            )

    def release(self):
        if self._acquired:
            self._acquired = False
            t0 = self._t0
            windll.kernel32.ReleaseMutex(self._mutex)
            
            if t0:
                elapsed = (datetime.datetime.now() - t0).total_seconds()
                if elapsed > 2:
                    from ..utils.logging import LogLevel, log
                    log(
                        f"Held lock for {self._mutex_name} during {elapsed:.1f} s",
                        LogLevel.TRACE,
                    )

    @staticmethod
    def for_path(path: Path):
        timeout_ms = 10 * 1000
        lock_name = str(path).replace(":", "-").replace("\\", "-")
        return FileLock(lock_name, timeout_ms)
