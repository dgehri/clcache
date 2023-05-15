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
SERVER_VERSION = "1"

PIPE_NAME = fr'\\.\pipe\LOCAL\clcache-626763c0-bebe-11ed-a901-0800200c9a66-{SERVER_VERSION}'
SINGLETON_NAME = fr"Local\singleton-626763c0-bebe-11ed-a901-0800200c9a66-{SERVER_VERSION}"
LAUNCH_MUTEX = fr"Local\mutex-626763c0-bebe-11ed-a901-0800200c9a66-{SERVER_VERSION}"
PIPE_READY_EVENT = fr"Local\ready-626763c0-bebe-11ed-a901-0800200c9a66-{SERVER_VERSION}"


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
    def __init__(self, timeout_s: int = 180):
        self._event_loop = None
        self._timer = None
        self._timeout = timeout_s
        self._pipe_server = None
        self._connections = []
        self._cache = None

    def run(self) -> int:
        # ensure only one instance of clcache server is running
        with AppSingleton(SINGLETON_NAME) as singleton:
            if not singleton.created():
                return 0

            self._event_loop = pyuv.Loop.default_loop()  # type: ignore
            self._cache = HashCache(self._event_loop)
            self._timer = pyuv.Timer(self._event_loop)  # type: ignore
            self._pipe_server = pyuv.Pipe(self._event_loop)  # type: ignore
            self._pipe_server.bind(PIPE_NAME)
            self._pipe_server.listen(self._on_connection)
            signal_handle = None
            try:
                signal_handle = pyuv.Signal(  # type: ignore
                    self._event_loop)
                signal_handle.start(PipeServer._on_sigterm, signal.SIGTERM)

                # start listening for connections, but stop event loop if idle
                # create a timer to stop the event loop if idle
                self._timer.start(self._on_timeout,
                                  self._timeout, self._timeout)

                # signal that the pipe is ready
                self._signal_server_ready()

                # start event loop
                self._event_loop.run()
                return 0
            finally:
                if signal_handle:
                    signal_handle.close()

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
                        raise runtime_error(response[1:-1].decode("utf-8"))
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

    @staticmethod
    def _signal_server_ready():
        # open existing event and set it
        ReadyEvent.signal(PIPE_READY_EVENT)

    @staticmethod
    def _on_timeout(handle: pyuv.Timer):  # type: ignore
        handle.loop.stop()

    def _on_connection(self, pipe, error):
        assert self._timer is not None
        assert self._cache is not None

        # reset timer
        self._timer.again()

        # accept connection
        client = pyuv.Pipe(self._pipe_server.loop)  # type: ignore
        pipe.accept(client)
        self._connections.append(Connection(
            client, self._cache, self._connections.remove))

    @staticmethod
    def _on_sigterm(handle, signum):
        for h in handle.loop.handles:
            h.close()


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
            if "__compiled__" not in globals():
                args.append(sys.executable)
            args.extend(
                (
                    Path(sys.argv[0]).absolute(),
                    f"--run-server={server_idle_timeout_s}",
                )
            )

            p = sp.Popen(
                args, creationflags=sp.CREATE_NEW_PROCESS_GROUP | sp.CREATE_NO_WINDOW)
            success = p.pid != 0 and ready_event.wait(wait_time_s*1000)
            if success:
                log(
                    f"Started hash server with timeout {server_idle_timeout_s} seconds")
            else:
                log("Failed to start hash server", level = LogLevel.WARN)
            return success
