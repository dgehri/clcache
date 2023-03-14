import ctypes
import functools
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from shutil import copyfile, copyfileobj, rmtree, which
from typing import Generator, Optional

import lz4.frame
import scandir

OUTPUT_LOCK = threading.Lock()


def print_binary(stream, data: bytes):
    with OUTPUT_LOCK:
        # split raw_data into chunks of 8192 bytes and write them to the stream
        for i in range(0, len(data), 8192):
            stream.buffer.write(data[i:i + 8192])

        stream.flush()


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


def find_compiler_binary() -> Optional[Path]:
    if "CLCACHE_CL" in os.environ:
        path: Path = Path(os.environ["CLCACHE_CL"])

        # If the path is not absolute, try to find it in the PATH
        if path.name == path:
            if p := which(path):
                path = Path(p)

        return path if path is not None and path.exists() else None

    return Path(p) if (p := which("cl.exe")) else None


def copy_from_cache(src_file: Path, dst_file: Path):
    '''
    Copy a file from the cache.

    Parameters:
        src_file_path: Path to the source file.
        dst_file_path: Path to the destination file.
    '''
    ensure_dir_exists(dst_file.absolute().parent)

    temp_dst: Path = dst_file.parent / f"{dst_file.name}.tmp"

    if os.path.exists(f"{src_file}.lz4"):
        with lz4.frame.open(f"{src_file}.lz4", mode="rb") as file_in:
            with open(temp_dst, "wb") as file_out:
                copyfileobj(file_in, file_out)  # type: ignore
    else:
        copyfile(src_file, temp_dst)

    temp_dst.replace(dst_file)


def copy_to_cache(src_file_path: Path, dst_file_path: Path) -> int:
    '''
    Copy a file to the cache.

    Parameters:
        src_file_path: Path to the source file.
        dst_file_path: Path to the destination file.

    Returns:
        The size of the file in bytes, after compression.
    '''
    ensure_dir_exists(dst_file_path.parent)

    temp_dst: Path = dst_file_path.parent / f"{dst_file_path.name}.tmp"
    dst_file_path = dst_file_path.parent / f"{dst_file_path.name}.lz4"
    with open(src_file_path, "rb") as file_in:
        with lz4.frame.open(temp_dst, mode="wb") as file_out:
            copyfileobj(file_in, file_out)  # type: ignore

    temp_dst.replace(dst_file_path)
    return dst_file_path.stat().st_size


def trace(msg: str, level=1) -> None:
    logLevel = int(os.getenv("CLCACHE_LOG", 0))
    if logLevel >= level:
        scriptDir = os.path.realpath(os.path.dirname(sys.argv[0]))
        with OUTPUT_LOCK:
            print(os.path.join(scriptDir, "clcache.py") + " " + msg, flush=True)


def error(message: str):
    with OUTPUT_LOCK:
        print(message, file=sys.stderr, flush=True)
