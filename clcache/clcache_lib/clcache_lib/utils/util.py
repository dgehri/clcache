from pathlib import Path
import sys
import threading
from typing import Generator, Optional
import lz4.frame
import os
from shutil import copyfile, copyfileobj, which
import scandir
from shutil import rmtree

OUTPUT_LOCK = threading.Lock()


def print_locked(stream, str: str):
    with OUTPUT_LOCK:
        # split raw_data into chunks of 8192 bytes and write them to the stream
        for i in range(0, len(str), 8192):
            stream.write(str[i:i + 8192])

        stream.flush()


def resolve(path: Path) -> Path:
    '''
    Resolve a path, caching the result for future calls.
    '''
    if path in resolve.cache:
        return resolve.cache[path]

    try:
        result = path.resolve()
        resolve.cache[path] = result
        resolve.cache[result] = result
        return result
    except Exception:
        resolve.cache[path] = path
        return path


resolve.cache = {}


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
    '''Iterate over lines in a string, separated by newline characters.'''
    pos = -1
    while True:
        next_pos = str.find('\n', pos + 1)
        if next_pos < 0:
            break
        yield str[pos + 1:next_pos]
        pos = next_pos


def line_iter_b(str: bytes) -> Generator[bytes, None, None]:
    '''Iterate over lines in a bytestring, separated by newline characters.'''
    pos = -1
    while True:
        next_pos = str.find(b'\n', pos + 1)
        if next_pos < 0:
            break
        yield str[pos + 1:next_pos]
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


def copy_or_link(src_file_path: Path, dst_file_path: Path, write_to_cache=False) -> int:
    ensure_dir_exists(dst_file_path.absolute().parent)

    temp_dst = dst_file_path.parent / f"{dst_file_path.name}.tmp"

    if write_to_cache is True:
        dst_file_path = dst_file_path.parent / f"{dst_file_path.name}.lz4"
        with open(src_file_path, "rb") as file_in, lz4.frame.open(
            temp_dst, mode="wb"
        ) as file_out:
            copyfileobj(file_in, file_out)  # type: ignore
    elif os.path.exists(f"{src_file_path}.lz4"):
        with lz4.frame.open(f"{src_file_path}.lz4", mode="rb") as file_in, open(
            temp_dst, "wb"
        ) as file_out:
            copyfileobj(file_in, file_out)  # type: ignore
    else:
        copyfile(src_file_path, temp_dst)

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
