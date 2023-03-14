# We often don't use all members of all the pyuv callbacks
# pylint: disable=unused-argument
import hashlib
import logging
import pickle
import signal
import subprocess as sp
import sys
from ctypes import windll, wintypes
from pathlib import Path
from typing import Callable

import pyuv

from ..config.config import VERSION

BUFFER_SIZE = 65536
PIPE_NAME = fr'\\.\pipe\LOCAL\clcache-626763c0-bebe-11ed-a901-0800200c9a66-{VERSION}'
SINGLETON_NAME = fr"Local\singleton-626763c0-bebe-11ed-a901-0800200c9a66-{VERSION}"
PIPE_READY_EVENT = fr"Local\ready-626763c0-bebe-11ed-a901-0800200c9a66-{VERSION}"

ERROR_SUCCESS = 0
ERROR_ALREADY_EXISTS = 0xB7


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
        handler = pyuv.fs.FSEvent(self._loop)
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
    def __init__(self, timeout_s: int = 60):
        self._event_loop = None
        self._timer = None
        self._timeout = timeout_s
        self._pipe_server = None
        self._connections = []
        self._cache = None

    def run(self) -> int:
        # ensure only one instance of clcache server is running
        event = None
        try:
            event = windll.kernel32.CreateEventW(None, wintypes.BOOL(True), wintypes.BOOL(
                False), SINGLETON_NAME)
            gle = windll.kernel32.GetLastError()
            if gle == ERROR_ALREADY_EXISTS or gle != ERROR_SUCCESS:
                return 1

            self._event_loop = pyuv.Loop.default_loop()  # type: ignore
            self._cache = HashCache(self._event_loop)
            self._timer = pyuv.Timer(self._event_loop)  # type: ignore
            self._pipe_server = pyuv.Pipe(self._event_loop)  # type: ignore
            self._pipe_server.bind(PIPE_NAME)
            self._pipe_server.listen(self._on_connection)
            signal_handle = None
            try:
                signal_handle = pyuv.Signal(self._event_loop)  # type: ignore
                signal_handle.start(PipeServer._on_sigterm, signal.SIGTERM)

                # start listening for connections, but stop event loop if idle for 60s
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
        finally:
            if event:
                windll.kernel32.CloseHandle(event)

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
    def _signal_server_ready():
        # open existing event and set it
        event = None
        try:
            event = windll.kernel32.OpenEventW(
                wintypes.DWORD(0x1F0003), wintypes.BOOL(False), PIPE_READY_EVENT)
            if event != 0:
                windll.kernel32.SetEvent(event)
        finally:
            if event:
                windll.kernel32.CloseHandle(event)

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


def spawn_server(server_idle_timeout_s: int, wait_time_s: int = 10):
    # if the server isn't running yet, spawn it
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
        return p.pid != 0 and ready_event.wait(wait_time_s*1000)
