import datetime
import enum
import multiprocessing
import sys
import threading
import weakref
from pathlib import Path

from ..config.config import VERSION
from ..utils.file_lock import FileLock


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
            try:
                # Failed, try printing to console
                for message in self._messages:
                    print(message, file=sys.stderr)
            except Exception:
                pass


def init_logger(log_dir: Path):
    if not log.messages:
        log.program_name = Path(sys.argv[0]).stem
        log_file = log_dir / f"{log.program_name }.log"
        log.messages = MessageBuffer(log_file)


def flush_logger():
    if log.messages:
        log.messages.flush()


def log(msg: str, level: LogLevel = LogLevel.TRACE, force_flush: bool = False) -> None:
    # sourcery skip: use-contextlib-suppress
    if log.messages is not None:
        try:
            # format message with process name, process id and trace level
            message = f'[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}] [{log.program_name} {VERSION}] [{multiprocessing.current_process().pid}] [{level.name}] {msg}'

            # accumulate messages in a list so that they can be printed later
            log.messages.append(message)

            if level >= LogLevel.WARN:
                from ..utils.win_evt_log import log_win_event

                log_win_event(message, level)

            if force_flush:
                log.messages.flush()
        except Exception:
            pass


log.messages = None
log.program_name = None
