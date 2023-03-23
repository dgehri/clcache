import datetime
import enum
import multiprocessing
import threading
import weakref
from pathlib import Path

from ..config.config import VERSION
from ..utils.file_lock import FileLock
from ..utils.util import get_program_name


class LogLevel(enum.IntEnum):
    TRACE = 1
    DEBUG = 2
    INFO = 3
    WARN = 4
    ERROR = 5


class MessageBuffer:
    def __init__(self, output_file: Path):
        self._finalizer = weakref.finalize(self, self.flush)
        self._messages = []
        self._lock = threading.Lock()
        self._output_file = output_file

    def append(self, message):
        with self._lock:
            self._messages.append(message)

    def flush(self):  # sourcery skip: use-contextlib-suppress
        try:
            with self._lock:
                with FileLock.for_path(self._output_file):
                    with open(self._output_file, "a") as f:
                        for message in self._messages:
                            # write message to file, separated by newline
                            f.write(f"{message}\n")
                self._messages.clear()
        except Exception:
            pass


def init_logger(log_dir: Path):
    if not log.messages:
        log.program_name = get_program_name()
        log_file = log_dir / f"{log.program_name }.log"
        log.messages = MessageBuffer(log_file)


def flush_logger():
    if log.messages:
        log.messages.flush()


def log(msg: str, level: LogLevel = LogLevel.TRACE) -> None:
    # sourcery skip: use-contextlib-suppress
    try:
        if log.messages is not None:
            # format message with process name, process id and trace level
            message = "[{0}] [{1}] [{2}] [{3}] {4}".format(
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                f"{log.program_name} {VERSION}",
                multiprocessing.current_process().pid,
                level.name,
                msg,
            )

            # accumulate messages in a list so that they can be printed later
            log.messages.append(message)
    except Exception:
        pass


log.messages = None
log.program_name = None
