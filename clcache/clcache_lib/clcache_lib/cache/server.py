import errno
import hashlib
import logging
import pickle
import signal
import subprocess as sp
import sys
from collections.abc import Callable
from ctypes import windll, wintypes
from pathlib import Path

import pyuv

from ..utils.app_singleton import AppSingleton
from ..utils.logging import LogLevel, log
from ..utils.named_mutex import NamedMutex
from ..utils.ready_event import ReadyEvent

# Not the same as VERSION !
SERVER_VERSION = "2"

UUID = fr'626763c0-bebe-11ed-a901-0800200c9a66-{SERVER_VERSION}'
PIPE_NAME = fr'\\.\pipe\LOCAL\clcache-{UUID}'
SINGLETON_NAME = fr"Local\singleton-{UUID}"
LAUNCH_MUTEX = fr"Local\mutex-{UUID}"
PIPE_READY_EVENT = fr"Local\ready-{UUID}"


BUFFER_SIZE = 65536
# Define some Win32 API constants here to avoid dependency on win32pipe
NMPWAIT_WAIT_FOREVER = wintypes.DWORD(0xFFFFFFFF)
ERROR_PIPE_BUSY = 231


class HashCache:
    def __init__(self, loop):
        self._loop = loop
        self._watched_dirs = {}
        self._handlers = []

    def get_file_hash(self, path: Path) -> str:
        watched_dir = self._watched_dirs.get(path.parent, {})

        if file_hash := watched_dir.get(path.name):
            # path in cache, check if it's still valid
            return file_hash

        # path not in cache or cache entry is invalid
        hasher = hashlib.md5()
        with open(path, "rb") as f:
            while chunk := f.read(128 * hasher.block_size):
                hasher.update(chunk)

        file_hash = hasher.hexdigest()

        watched_dir[path.name] = file_hash
        if path.parent not in self._watched_dirs:
            self._start_watching(path.parent)
        self._watched_dirs[path.parent] = watched_dir

        return file_hash

    def _start_watching(self, directory: Path):
        handler = pyuv.fs.FSEvent(self._loop)  # type: ignore
        handler.start(str(directory), 0, self._on_path_change)
        self._handlers.append(handler)

    def _on_path_change(self, handler, filename, events, error):
        directory = Path(handler.path)
        watched_dir = self._watched_dirs.get(directory, {})
        if filename in watched_dir:
            del watched_dir[filename]

            if not watched_dir:
                handler.stop()
                self._handlers.remove(handler)
                del self._watched_dirs[directory]

    def __del__(self):
        for ev in self._handlers:
            ev.stop()


class Connection:
    def __init__(self,
                 pipe: pyuv.Pipe,  # type: ignore
                 cache: HashCache,
                 on_close_callback: Callable):
        self._read_buf: bytes = b''
        self._pipe = pipe
        self._cache = cache
        self._on_close_callback = on_close_callback
        pipe.start_read(self._on_client_read)

    def _on_client_read(self, pipe: pyuv.Pipe, data: bytes, error):  # type: ignore
        self._read_buf += data
        if self._read_buf.endswith(b'\x00'):
            paths = map(Path, self._read_buf[:-1].decode('utf-8').splitlines())
            try:
                hashes = map(self._cache.get_file_hash, paths)
                response = '\n'.join(hashes).encode('utf-8')
            except OSError as e:
                response = b'!' + pickle.dumps(e)
            pipe.write(response + b'\x00', self._on_write_done)

    def _on_write_done(self, pipe, error):
        logging.debug("sent response to client, closing connection")
        self._pipe.close()
        self._on_close_callback(self)


class PipeServer:

    @staticmethod
    def is_running():
        event = None
        try:
            event = windll.kernel32.OpenEventW(
                wintypes.DWORD(0x1F0003), wintypes.BOOL(False), SINGLETON_NAME)
            return event != 0
        finally:
            if event:
                windll.kernel32.CloseHandle(event)

    @staticmethod
    def get_file_hashes(path_list: list[Path]) -> list[str]:
        """Get file hashes from clcache server."""
        while True:
            try:
                with open(PIPE_NAME, "w+b") as f:
                    f.write("\n".join(map(str, path_list)).encode("utf-8"))
                    f.write(b"\x00")
                    response = f.read()
                    if response.startswith(b"!"):
                        # extract error string
                        error = response[1:-1].decode("utf-8")
                        raise FileNotFoundError(error)
                    return response[:-1].decode("utf-8").splitlines()
            except OSError as e:
                if (
                    e.errno == errno.EINVAL
                    and windll.kernel32.GetLastError() == ERROR_PIPE_BUSY
                ):
                    # All pipe instances are busy, wait until available
                    windll.kernel32.WaitNamedPipeW(
                        PIPE_NAME, NMPWAIT_WAIT_FOREVER)
                else:
                    raise


def spawn_server(server_idle_timeout_s: int, wait_time_s: int = 10):
    # sourcery skip: extract-method

    # if the server is already running, return immediately
    if PipeServer.is_running():
        return True

    # avoid dobule spawning
    with NamedMutex(LAUNCH_MUTEX):

        # if the server is already running, return immediately
        if PipeServer.is_running():
            return True

        with ReadyEvent(PIPE_READY_EVENT) as ready_event:
            args = []
            # Launch clcache_server.exe located in the same directory as this executable
            if "__compiled__" not in globals():
                args.append(Path(sys.argv[0]).absolute().parent /
                            "clcache_server/target/release/clcache_server.exe")
            else:
                args.append(Path(sys.executable).parent / "clcache_server.exe")

            args.extend((f"--idle-timeout={server_idle_timeout_s}", f"--id={UUID}"))
            try:
                p = sp.Popen(
                    args, creationflags=sp.CREATE_NEW_PROCESS_GROUP | sp.CREATE_NO_WINDOW)
                success = p.pid != 0 and ready_event.wait(wait_time_s*1000)
                if success:
                    log(
                        f"Started hash server with timeout {server_idle_timeout_s} seconds")
                else:
                    log("Failed to start hash server", level=LogLevel.WARN)
            except FileNotFoundError as e:
                raise RuntimeError(
                    "Failed to start hash server: clcache_server.exe not found"
                ) from e
            return success
