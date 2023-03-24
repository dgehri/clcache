import ctypes
import functools
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from shutil import rmtree
from typing import Generator

import scandir

from ..utils.logging import log

OUTPUT_LOCK = threading.Lock()


def get_program_name() -> str:
    return Path(sys.argv[0]).stem


def _print_binary(stream, data: bytes):
    with OUTPUT_LOCK:
        # split raw_data into chunks of 8192 bytes and write them to the stream
        for i in range(0, len(data), 8192):
            stream.buffer.write(data[i:i + 8192])

        stream.flush()


def print_stdout_and_stderr(out: str, err: str, encoding: str):
    _print_binary(sys.stdout, out.encode(encoding))
    _print_binary(sys.stderr, err.encode(encoding))


@functools.cache
def resolve(path: Path) -> Path:
    '''Resolve a path, caching the result for future calls.'''

    try:
        return path.resolve()
    except Exception:
        return path


_GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW
_GetShortPathNameW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
_GetShortPathNameW.restype = wintypes.DWORD
_GetLongPathNameW = ctypes.windll.kernel32.GetLongPathNameW
_GetLongPathNameW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
_GetLongPathNameW.restype = wintypes.DWORD


def get_short_path_name(path: Path) -> Path:
    """
    Get the short path name of a path

        Parameters:
            path (str): the path to get the short path name for

        Returns:
            str: the short path name
    """
    path_str = str(path)
    output_buf_size = len(path_str)
    while True:
        output_buf = ctypes.create_unicode_buffer(output_buf_size)
        needed = _GetShortPathNameW(path_str, output_buf, output_buf_size)
        if output_buf_size >= needed:
            return Path(output_buf.value)
        elif needed == 0:
            return path
        else:
            output_buf_size = needed


def get_long_path_name(path: Path) -> Path:
    """
    Get the long path name of a path

        Parameters:
            path (str): the path to get the long path name for

        Returns:
            str: the long path name
    """
    path_str = str(path)
    output_buf_size = len(path_str)
    while True:
        output_buf = ctypes.create_unicode_buffer(output_buf_size)
        needed = _GetLongPathNameW(path_str, output_buf, output_buf_size)
        if output_buf_size >= needed:
            return Path(output_buf.value)
        elif needed == 0:
            return path
        else:
            output_buf_size = needed


def files_beneath(base_dir: Path) -> Generator[Path, None, None]:
    for path, _, filenames in scandir.walk(str(base_dir)):
        for filename in filenames:
            yield Path(path) / filename


def child_dirs_str(path: str, absolute=True) -> Generator[str, None, None]:
    """Return a generator of child directories of the given path, recursively."""
    for entry in scandir.scandir(path):
        if entry.is_dir():
            yield entry.path if absolute else entry.name


def child_dirs(path: Path, absolute=True) -> Generator[Path, None, None]:
    """Return a generator of child directories of the given path, recursively."""
    for entry in scandir.scandir(str(path)):
        if entry.is_dir():
            yield Path(entry.path) if absolute else Path(entry.name)


def remove_and_recreate_dir(path: Path):
    '''
    Remove directory if it exists and create a new one.
    '''
    rmtree(path, ignore_errors=True)
    ensure_dir_exists(path)


def ensure_dir_exists(path: Path):
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        pass
    except Exception:
        raise


def line_iter(str: str, strip=False) -> Generator[str, None, None]:
    '''Iterate over lines in a string, separated by newline characters.'''
    pos = -1
    while True:
        next_pos = str.find('\n', pos + 1)
        if next_pos < 0:
            break
        line = str[pos + 1:next_pos]
        yield line.rstrip("\r\n") if strip else line
        pos = next_pos


def line_iter_b(str: bytes, strip=False) -> Generator[bytes, None, None]:
    '''Iterate over lines in a bytestring, separated by newline characters.'''
    pos = -1
    while True:
        next_pos = str.find(b'\n', pos + 1)
        if next_pos < 0:
            break
        line = str[pos + 1:next_pos]
        yield line.rstrip(b"\r\n") if strip else line
        pos = next_pos


@functools.cache
def get_build_dir() -> Path:
    '''
    Get the build directory.

    Get the build directory from the CLCACHE_BUILDDIR environment 
    variable. If it is not set, use the current working directory 
    to determine it.
    '''
    def impl():
        if value := os.environ.get("CLCACHE_BUILDDIR"):
            build_dir = Path(value)
            if build_dir.exists():
                return normalize_dir(build_dir)

        # walk up the directory tree, starting at the current working directory,
        # to find the build directory, as determined by the existence of the
        # CMakeCache.txt file
        for path in [Path.cwd()] + list(Path.cwd().parents):
            if (path / "CMakeCache.txt").exists():
                return normalize_dir(path)

        return normalize_dir(Path.cwd())

    result = impl()
    log(f"<BUILDDIR> = {result}")
    return result


@functools.cache
def normalize_dir(dir_path: Path) -> Path:
    '''
    Normalize a directory path, removing trailing slashes.

    This is a workaround for https://bugs.python.org/issue9949
    '''
    result = os.path.normcase(os.path.abspath(os.path.normpath(str(dir_path))))
    if result.endswith(os.path.sep):
        result = result[:-1]
    return Path(result)
