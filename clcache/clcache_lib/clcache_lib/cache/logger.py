# We often don't use all members of all the pyuv callbacks
import errno
import hashlib
import pickle
import signal
import subprocess as sp
import sys
import threading
from ctypes import windll, wintypes
from pathlib import Path
from typing import Callable

import pyuv

from ..utils.app_singleton import AppSingleton
from ..utils.logging import log, LogLevel
from ..utils.named_mutex import NamedMutex
from ..utils.ready_event import ReadyEvent

# Not the same as VERSION !
SERVER_VERSION = "1"

PIPE_NAME = fr'\\.\pipe\LOCAL\clcache-835c7daa-96ee-4307-8533-55348ba3ed22-{SERVER_VERSION}'
SINGLETON_NAME = fr"Local\singleton-835c7daa-96ee-4307-8533-55348ba3ed22-{SERVER_VERSION}"
LAUNCH_MUTEX = fr"Local\mutex-835c7daa-96ee-4307-8533-55348ba3ed22-{SERVER_VERSION}"
PIPE_READY_EVENT = fr"Local\ready-835c7daa-96ee-4307-8533-55348ba3ed22-{SERVER_VERSION}"


BUFFER_SIZE = 65536
NMPWAIT_WAIT_FOREVER = wintypes.DWORD(0xFFFFFFFF)
ERROR_PIPE_BUSY = 231


class LogSink:
    """Class for writing log messages to a file."""

    def __init__(self, log_path: Path):
        self._file = open(log_path, "w")
        self._lock = threading.Lock()

    def write(self, message: str):
        with self._lock:
            if self._file:
                self._file.write(message)
                self._file.flush()

    def close(self):
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None

    def __del__(self):
        self.close()


class Connection:
    def __init__(self,
                 pipe: pyuv.Pipe, # type: ignore
                 sink: LogSink,
                 on_close_callback: Callable):
        self._read_buf: bytes = b''
        self._pipe = pipe
        self._sink = sink
        self._on_close_callback = on_close_callback
        pipe.start_read(self._on_client_read)

    def _on_client_read(self, pipe: pyuv.Pipe, data: bytes, error):  # type: ignore
        self._read_buf += data
        if self._read_buf.endswith(b'\x00'):
            self._sink.write(self._read_buf[:-1].decode('utf-8'))

    def _on_write_done(self, pipe, error):
        self._pipe.close()
        self._on_close_callback(self)


class LoggerServer:
    def __init__(self, log_file: Path, timeout_s: int = 180):
        self._event_loop = None
        self._timer = None
        self._log_file = log_file
        self._timeout = timeout_s
        self._pipe_server = None
        self._connections = []
        self._sink = None

    def run(self) -> int:
        # ensure only one instance of clcache server is running
        with AppSingleton(SINGLETON_NAME) as singleton:
            if not singleton.created():
                return 0

            self._event_loop = pyuv.Loop.default_loop()  # type: ignore
            self._sink = LogSink(self._log_file)
            self._timer = pyuv.Timer(self._event_loop)  # type: ignore
            self._pipe_server = pyuv.Pipe(self._event_loop)  # type: ignore
            self._pipe_server.bind(PIPE_NAME)
            self._pipe_server.listen(self._on_connection)
            signal_handle = None
            try:
                signal_handle = pyuv.Signal(  # type: ignore
                    self._event_loop)
                signal_handle.start(LoggerServer._on_sigterm, signal.SIGTERM)

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
    def log_message(message: str):
        while True:
            try:
                with open(PIPE_NAME, "w+b") as f:
                    f.write(f"{message}\n".encode("utf-8"))
                    f.write(b"\x00")
            except OSError as e:
                if (
                    e.errno == errno.EINVAL
                    and windll.kernel32.GetLastError() == ERROR_PIPE_BUSY
                ):
                    # All pipe instances are busy, wait until available
                    windll.kernel32.WaitNamedPipeW(
                        PIPE_NAME, NMPWAIT_WAIT_FOREVER)
                else:
                    break # pipe not available, ignore

    @staticmethod
    def _signal_server_ready():
        # open existing event and set it
        ReadyEvent.signal(PIPE_READY_EVENT)

    @staticmethod
    def _on_timeout(handle: pyuv.Timer):  # type: ignore
        handle.loop.stop()

    def _on_connection(self, pipe, error):
        assert self._timer is not None
        assert self._sink is not None

        # reset timer
        self._timer.again()

        # accept connection
        client = pyuv.Pipe(self._pipe_server.loop)  # type: ignore
        pipe.accept(client)
        self._connections.append(Connection(
            client, self._sink, self._connections.remove))

    @staticmethod
    def _on_sigterm(handle, signum):
        for h in handle.loop.handles:
            h.close()


def spawn_server(log_path: Path, wait_time_s: int = 10):
    # sourcery skip: extract-method

    # if the server is already running, return immediately
    if LoggerServer.is_running():
        return True

    # avoid dobule spawning
    with NamedMutex(LAUNCH_MUTEX):

        # if the server is already running, return immediately
        if LoggerServer.is_running():
            return True

        with ReadyEvent(PIPE_READY_EVENT) as ready_event:
            args = []
            if "__compiled__" not in globals():
                args.append(sys.executable)
            args.extend(
                (
                    Path(sys.argv[0]).absolute(),
                    f"--run-logger=\"{log_path}\"",
                )
            )

            p = sp.Popen(
                args, creationflags=sp.CREATE_NEW_PROCESS_GROUP | sp.CREATE_NO_WINDOW)
            success = p.pid != 0 and ready_event.wait(wait_time_s*1000)
            if success:
                log("Started logger server")
            else:
                log("Failed to start logger server", level=LogLevel.ERROR)
            return success
