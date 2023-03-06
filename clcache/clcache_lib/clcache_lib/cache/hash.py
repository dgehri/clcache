import functools
import hashlib
import os
import errno
from pathlib import Path
import pickle
from ctypes import windll, wintypes
from typing import List, Optional

from ..config import CACHE_VERSION
from .virt import subst_basedir_with_placeholder, is_in_build_dir

HashAlgorithm = hashlib.md5
BUFFER_SIZE = 65536

# Define some Win32 API constants here to avoid dependency on win32pipe
NMPWAIT_WAIT_FOREVER = wintypes.DWORD(0xFFFFFFFF)
ERROR_PIPE_BUSY = 231


def get_compiler_hash(compiler_path: Path) -> str:
    '''
    Returns the hash of the given compiler executable.

    The hash is based on the file modification time, file 
    size and the cache version.
    '''
    stat = os.stat(compiler_path)
    data = "|".join(
        [
            str(stat.st_mtime),
            str(stat.st_size),
            CACHE_VERSION,
        ]
    )

    return get_string_hash(data)


def get_file_hashes(path_list: List[Path]) -> List[str]:
    '''
    Returns the hashes of the given files.

    Parameters:
        path_list: The paths of the files to hash.

    Returns:
        The hashes of the files.
    '''
    # Try fetching the hashes from the server
    if "CLCACHE_SERVER" not in os.environ:
        return [get_file_hash(path) for path in path_list]

    # If the server is not available, fall back to the local cache
    pipe_name = r"\\.\pipe\clcache_srv"
    while True:
        try:
            with open(pipe_name, "w+b") as f:
                f.write("\n".join(str(path_list)).encode("utf-8"))
                f.write(b"\x00")
                response = f.read()
                if response.startswith(b"!"):
                    raise pickle.loads(response[1:-1])
                return response[:-1].decode("utf-8").splitlines()
        except OSError as e:
            if (
                e.errno == errno.EINVAL
                and windll.kernel32.GetLastError() == ERROR_PIPE_BUSY
            ):
                windll.kernel32.WaitNamedPipeW(pipe_name, NMPWAIT_WAIT_FOREVER)
            else:
                raise


@functools.cache
def get_file_hash(path: Path, toolset_data: Optional[str] = None) -> str:
    '''
    Returns the hash of the given file.

    Parameters:
        path: The path of the file to hash.
        toolset_data: Additional data to include in the hash.

    Returns:
        The hash of the file.
    '''

    hasher = HashAlgorithm()

    with open(path, "rb") as f:
        if not is_in_build_dir(path):
            b = f.read(BUFFER_SIZE)
            while len(b) > 0:
                hasher.update(b)
                b = f.read(BUFFER_SIZE)
        else:
            # If the file is in the build directory, it may contain references
            # (includes, comments) to the files in the base (source) directory.
            # We need to replace those references with a placeholder to make the
            # hash independent of that information.
            src_dir = path.parent  # get containing folder of path
            src_content = subst_basedir_with_placeholder(f.read(),  src_dir)
            hasher.update(src_content)

    # trace(f"File hash: {filePath} => {hasher.hexdigest()}", 2)

    if toolset_data is not None:
        # Encoding of this additional data does not really matter
        # as long as we keep it fixed, otherwise hashes change.
        # The string should fit into ASCII, so UTF8 should not change anything
        hasher.update(toolset_data.encode("UTF-8"))
        # trace(f"AdditionalData Hash: {hasher.hexdigest()}: {additionalData}", 2)

    return hasher.hexdigest()


def get_string_hash(data: str):
    hasher = HashAlgorithm()
    hasher.update(data.encode("UTF-8"))
    return hasher.hexdigest()
