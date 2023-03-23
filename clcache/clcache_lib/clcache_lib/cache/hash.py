import functools
import hashlib
import os
import traceback
from pathlib import Path
from typing import List, Optional

from ..cache.server import PipeServer, spawn_server
from ..config import CACHE_VERSION
from ..config.config import HASH_SERVER_TIMEOUT
from ..utils.logging import LogLevel, log
from .virt import is_in_build_dir, subst_basedir_with_placeholder

HashAlgorithm = hashlib.md5


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


def _get_sever_timeout_seconds() -> Optional[int]:
    '''
    Returns the timeout for the hash server in seconds.

    Returns None if the timeout is not set or invalid, or less than 1 minute.
    '''
    # ignore exception if not a valid int
    try:
        if env_value := os.environ.get("CLCACHE_SERVER_TIMEOUT_MINUTES"):
            minutes = int(env_value)
            return minutes * 60 if minutes > 0 else None
        return HASH_SERVER_TIMEOUT.seconds

    except ValueError:
        return None


def get_file_hashes(path_list: List[Path]) -> List[str]:
    '''
    Returns the hashes of the given files.

    Parameters:
        path_list: The paths of the files to hash.

    Returns:
        The hashes of the files.
    '''

    if server_timeout_secs := _get_sever_timeout_seconds():
        try:
            if not spawn_server(server_timeout_secs):
                raise OSError("Server didn't start in time")

            return PipeServer.get_file_hashes(path_list)
        except Exception as e:
            log(f"Failed to use server: {traceback.format_exc()}", LogLevel.ERROR)

    return [get_file_hash(path) for path in path_list]


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
            while chunk := f.read(128 * hasher.block_size):
                hasher.update(chunk)
        else:
            # If the file is in the build directory, it may contain references
            # (includes, comments) to the files in the base (source) directory.
            # We need to replace those references with a placeholder to make the
            # hash independent of that information.
            src_dir = path.parent  # get containing folder of path
            src_content = subst_basedir_with_placeholder(f.read(),  src_dir)
            hasher.update(src_content)

    # log(f"File hash: {path.as_posix()} => {hasher.hexdigest()}", 2)

    if toolset_data is not None:
        # Encoding of this additional data does not really matter
        # as long as we keep it fixed, otherwise hashes change.
        # The string should fit into ASCII, so UTF8 should not change anything
        hasher.update(toolset_data.encode("UTF-8"))
        # log(f"AdditionalData Hash: {hasher.hexdigest()}: {toolset_data}", 2)

    return hasher.hexdigest()


def get_string_hash(data: str):
    hasher = HashAlgorithm()
    hasher.update(data.encode("UTF-8"))
    return hasher.hexdigest()
