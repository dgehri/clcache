import datetime
import enum
import multiprocessing
import sys
import threading
import traceback
import weakref
from pathlib import Path

import win32con
import win32evtlogutil

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
            message = "[{0}] [{1}] [{2}] [{3}] {4}".format(
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                f"{log.program_name} {VERSION}",
                multiprocessing.current_process().pid,
                level.name,
                msg,
            )

            # accumulate messages in a list so that they can be printed later
            log.messages.append(message)

            if force_flush:
                log.messages.flush()
        except Exception:
            pass


log.messages = None
log.program_name = None


def log_win_event(message: str):  # sourcery skip: use-contextlib-suppress
    try:
        if log_win_event.program_name is None:
            log_win_event.program_name = Path(sys.argv[0]).stem

        if log_win_event.pid is None:
            log_win_event.pid = multiprocessing.current_process().pid

        event_id = 1000
        event_type = win32con.EVENTLOG_INFORMATION_TYPE
        win32evtlogutil.ReportEvent(
            appName=log_win_event.program_name, eventID=event_id, eventType=event_type, strings=[f"[{log_win_event.pid}] {message}"], data=b"")
    except Exception:
        pass


log_win_event.program_name = None
log_win_event.pid = None

# Function to log message to a file in the temp folder, named after the program and PID.
# The file is opened and closed for each message to ensure that it is written to disk.
def log_message_to_file(message: str):  # sourcery skip: use-contextlib-suppress
    try:
        # acquire lock to ensure that only one thread writes to the file at a time
        with log_message_to_file.lock:
            if log_message_to_file.program_name is None:
                log_message_to_file.program_name = Path(sys.argv[0]).stem

            if log_message_to_file.pid is None:
                log_message_to_file.pid = multiprocessing.current_process().pid

            log_file = Path(
                f"{log_message_to_file.program_name}_{log_message_to_file.pid}.log")
            with open(log_file, "a") as f:
                f.write(f"{message}\n")
    except Exception:
        pass
    
log_message_to_file.program_name = None
log_message_to_file.pid = None
log_message_to_file.lock = threading.Lock()
log_message_to_file.nesting = 0

class log_method_call:
    def __init__(self, func):
        self.func = func
        for attr in dir(func):
            if not attr.startswith("__"):
                setattr(self, attr, getattr(func, attr))

    def __call__(self, *args, **kwargs):
        try:
            log_message_to_file(f"{'  ' * log_message_to_file.nesting}[[[[{self.func.__name__}: {args} / {kwargs}")
            log_message_to_file.nesting += 1
            result = self.func(*args, **kwargs)
            log_message_to_file.nesting -= 1
            log_message_to_file(f"{'  ' * log_message_to_file.nesting}]]]]")
        except Exception as e:
            stack_trace = traceback.format_exc()
            log_message_to_file(f"{'  ' * log_message_to_file.nesting}!!!!{self.func.__name__}: {stack_trace}")
            raise
        return result
    
def apply_decorator_to_module_functions(module):
    import contextlib
    import importlib
    import inspect
    for name in dir(module):
        obj = getattr(module, name)
        if callable(obj) and not inspect.isclass(obj):
            if name not in ['log_method_call', 'log_win_event', 'log', 'flush_logger', '_combine', 'log_message_to_file']:
                setattr(module, name, log_method_call(obj))
        elif inspect.ismodule(obj):
            with contextlib.suppress(ImportError):
                sub_module = importlib.import_module(f'{module.__name__}.{name}')
                apply_decorator_to_module_functions(sub_module)