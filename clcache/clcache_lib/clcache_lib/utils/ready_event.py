from ctypes import windll, wintypes


class ReadyEvent:
    def __init__(self, name: str):
        self._event = None
        self._name = name

    def __enter__(self):
        self._event = windll.kernel32.CreateEventW(None, wintypes.BOOL(True), wintypes.BOOL(
            False), self._name)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._event:
            windll.kernel32.CloseHandle(self._event)
            self._event = None

    def wait(self, timeout_ms: int) -> bool:
        if self._event != 0:
            return windll.kernel32.WaitForSingleObject(self._event, timeout_ms) == 0
        return False

    @staticmethod
    def signal(name: str):
        event = None
        try:
            event = windll.kernel32.OpenEventW(
                wintypes.DWORD(0x1F0003), wintypes.BOOL(False), name)
            if event != 0:
                windll.kernel32.SetEvent(event)
        finally:
            if event:
                windll.kernel32.CloseHandle(event)
