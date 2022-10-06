from ctypes import windll, wintypes
from .ex import CacheLockException

class CacheLock:
    """Implements a lock for the object cache which
    can be used in 'with' statements."""

    INFINITE = 0xFFFFFFFF
    WAIT_ABANDONED_CODE = 0x00000080
    WAIT_TIMEOUT_CODE = 0x00000102

    def __init__(self, mutexName, timeoutMs):
        self._mutexName = "Local\\" + mutexName
        self._mutex = None
        self._timeoutMs = timeoutMs

    def createMutex(self):
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
        if not self._mutex:
            self.createMutex()
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
            raise CacheLockException(errorString)

    def release(self):
        windll.kernel32.ReleaseMutex(self._mutex)

    @staticmethod
    def forPath(path):
        timeoutMs = 10 * 1000
        lockName = path.replace(":", "-").replace("\\", "-")
        return CacheLock(lockName, timeoutMs)
