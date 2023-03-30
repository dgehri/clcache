import ctypes
import functools
import glob
import os
import re
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from shutil import rmtree
from collections.abc import Generator

import scandir

OUTPUT_LOCK = threading.Lock()


def get_program_name() -> str:
    return Path(sys.argv[0]).stem


def print_binary(stream, data: bytes):
    with OUTPUT_LOCK:
        stream.buffer.write(data)
        stream.flush()


def print_stdout_and_stderr(out: str, err: str, encoding: str):
    print_binary(sys.stdout, out.encode(encoding))
    print_binary(sys.stderr, err.encode(encoding))


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


def line_iter(str: str) -> Generator[str, None, None]:
    '''
    Iterate over lines in a string, separated by newline characters.

    The returned lines include the newline character.
    '''
    pos = 0
    while True:
        next_pos = str.find('\n', pos)
        if next_pos < 0:
            yield str[pos:]
            break
        yield str[pos:next_pos+1]
        pos = next_pos+1


def line_iter_b(str: bytes) -> Generator[bytes, None, None]:
    '''
    Iterate over lines in a string, separated by newline characters.

    The returned lines include the newline character.
    '''
    pos = 0
    while True:
        next_pos = str.find(b'\n', pos)
        if next_pos < 0:
            yield str[pos:]
            break
        yield str[pos:next_pos+1]
        pos = next_pos+1


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


def correct_case_path(path: Path) -> Path:
    pattern = re.sub(r'([^:/\\])(?=[/\\]|$)', r'[\1]', os.path.normpath(path))
    return Path(r[0]) if (r := glob.glob(pattern)) else path
