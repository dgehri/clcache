from pathlib import Path
import sys
import threading
import errno
import lz4.frame
import os
from pathlib import Path
from shutil import copyfile, copyfileobj, which

OUTPUT_LOCK = threading.Lock()


# try to use os.scandir or scandir.scandir
# fall back to os.listdir if not found
# same for scandir.walk
try:
    import scandir  # pylint: disable=wrong-import-position

    WALK = scandir.walk
    LIST = scandir.scandir
except ImportError:
    WALK = os.walk
    try:
        LIST = os.scandir  # type: ignore # pylint: disable=no-name-in-module
    except AttributeError:
        LIST = os.listdir


def get_actual_filename(name):
    try:
        return str(Path(name).resolve())
    except Exception:
        return name


def print_binary(stream, rawData):
    with OUTPUT_LOCK:
        stream.buffer.write(rawData)
        stream.flush()


def basename_without_extension(path):
    basename = os.path.basename(path)
    return os.path.splitext(basename)[0]


def files_beneath(baseDir):
    for path, _, filenames in WALK(baseDir):
        for filename in filenames:
            yield os.path.join(path, filename)


def child_dirs(path, absolute=True):
    supportsScandir = LIST != os.listdir
    for entry in LIST(path):
        if supportsScandir:
            if entry.is_dir():
                yield entry.path if absolute else entry.name
        else:
            absPath = os.path.join(path, entry)
            if os.path.isdir(absPath):
                yield absPath if absolute else entry


def normalize_dir(dir_path):
    if not dir_path:
        return None
    dir_path = os.path.normcase(os.path.abspath(os.path.normpath(dir_path)))
    if dir_path.endswith(os.path.sep):
        dir_path = dir_path[:-1]
    return dir_path


def ensure_dir_exists(path):
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def my_executable_path():
    assert hasattr(sys, "frozen"), "is not frozen by py2exe"
    return sys.executable.upper()


def find_compiler_binary():
    if "CLCACHE_CL" in os.environ:
        path = os.environ["CLCACHE_CL"]
        if os.path.basename(path) == path:
            path = which(path)

        return path if os.path.exists(path) else None

    frozen_by_py2_exe = hasattr(sys, "frozen")

    for p in os.environ["PATH"].split(os.pathsep):
        path = os.path.join(p, "cl.exe")
        if os.path.exists(path):
            if not frozen_by_py2_exe:
                return path

            # Guard against recursively calling ourselves
            if path.upper() != my_executable_path():
                return path
    return None


def copy_or_link(src_file_path, dst_file_path, write_to_cache=False):
    ensure_dir_exists(os.path.dirname(os.path.abspath(dst_file_path)))

    temp_dst = f"{dst_file_path}.tmp"

    if write_to_cache is True:
        dst_file_path += ".lz4"
        with open(src_file_path, "rb") as file_in, lz4.frame.open(
            temp_dst, mode="wb"
        ) as file_out:
            copyfileobj(file_in, file_out)
    elif os.path.exists(f"{src_file_path}.lz4"):
        with lz4.frame.open(f"{src_file_path}.lz4", mode="rb") as file_in, open(
            temp_dst, "wb"
        ) as file_out:
            copyfileobj(file_in, file_out)
    else:
        copyfile(src_file_path, temp_dst)

    os.replace(temp_dst, dst_file_path)
    return os.path.getsize(dst_file_path)


def trace(msg: str, level=1) -> None:

    logLevel = int(os.getenv("CLCACHE_LOG")) if "CLCACHE_LOG" in os.environ else 0

    if logLevel >= level:
        scriptDir = os.path.realpath(os.path.dirname(sys.argv[0]))
        with OUTPUT_LOCK:
            print(os.path.join(scriptDir, "clcache.py") + " " + msg, flush=True)


def error(message):
    with OUTPUT_LOCK:
        print(message, file=sys.stderr, flush=True)
