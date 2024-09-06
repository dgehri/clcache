from ctypes import windll, wintypes

ERROR_SUCCESS = 0
ERROR_ALREADY_EXISTS = 0xB7


class AppSingleton:
    """Singleton for controlling application instances."""

    def __init__(self, name: str):
        self.name = name
        self.event = None

    def create(self) -> bool:
        self.event = windll.kernel32.CreateEventW(None, wintypes.BOOL(True), wintypes.BOOL(
            False), self.name)
        gle = windll.kernel32.GetLastError()
        return gle != ERROR_ALREADY_EXISTS and gle == ERROR_SUCCESS

    def destroy(self):
        if self.event:
            windll.kernel32.CloseHandle(self.event)
            self.event = None

    def created(self) -> bool:
        return self.event != None

    __del__ = destroy

    def __enter__(self):
        self.create()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass
