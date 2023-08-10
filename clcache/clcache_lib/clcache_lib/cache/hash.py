import functools
import hashlib
import os
import traceback
from pathlib import Path

from ..cache.server import PipeServer, spawn_server
from ..config import CACHE_VERSION
from ..config.config import HASH_SERVER_TIMEOUT
from ..utils.logging import LogLevel, log
from .virt import is_in_build_dir, subst_basedir_with_placeholder

HashAlgorithm = hashlib.md5


def get_compiler_hash(compiler_path: Path) -> str:
    """
    Returns the hash of the given compiler executable.

    The hash is based on the file modification time, file
    size and the cache version.
    """
    stat = os.stat(compiler_path)
    data = "|".join(
        [
            str(stat.st_mtime),
            str(stat.st_size),
            CACHE_VERSION,
        ]
    )

    return get_string_hash(data)


def _get_sever_timeout_seconds() -> int | None:
    """
    Returns the timeout for the hash server in seconds.

    Returns None if the timeout is not set or invalid, or less than 1 minute.
    """
    # ignore exception if not a valid int
    try:
        if env_value := os.environ.get("CLCACHE_SERVER_TIMEOUT_MINUTES"):
            minutes = int(env_value)
            return minutes * 60 if minutes > 0 else None
        return HASH_SERVER_TIMEOUT.seconds

    except ValueError:
        return None


def get_file_hashes(path_list: list[Path]) -> list[str]:
    """
    Returns the hashes of the given files.

    Parameters:
        path_list: The paths of the files to hash.

    Returns:
        The hashes of the files.
    """

    # if CLCACHE_SERVER_DISABLE is set, don't use the server
    if not os.environ.get("CLCACHE_SERVER_DISABLE"):
        if server_timeout_secs := _get_sever_timeout_seconds():
            try:
                return _get_file_hashes_from_server(server_timeout_secs, path_list)
            except FileNotFoundError:
                # This is expected
                raise
            except Exception as e:
                log(f"Failed to use server: {traceback.format_exc()}", LogLevel.ERROR)

    return [get_file_hash(path) for path in path_list]


def _get_file_hashes_from_server(server_timeout_secs, path_list):
    if not spawn_server(server_timeout_secs):
        raise OSError("Server didn't start in time")

    # Split path_list into paths in build dir and paths not in build dir,
    # and remember original index into original list
    build_dir_paths = []
    other_paths = []
    is_build_dir = []
    for path in path_list:
        if is_in_build_dir(path):
            build_dir_paths.append(path)
            is_build_dir.append(True)
        else:
            other_paths.append(path)
            is_build_dir.append(False)

    other_hashes = PipeServer.get_file_hashes(other_paths)
    build_dir_hashes = [get_file_hash(path) for path in build_dir_paths]

    # Recombine hashes in original order
    hashes = []
    for i in range(len(path_list)):
        if is_build_dir[i]:
            hashes.append(build_dir_hashes.pop(0))
        else:
            hashes.append(other_hashes.pop(0))
    return hashes


@functools.cache
def get_file_hash(path: Path, toolset_data: str | None = None) -> str:
    """
    Returns the hash of the given file.

    Parameters:
        path: The path of the file to hash.
        toolset_data: Additional data to include in the hash.

    Returns:
        The hash of the file.
    """

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
            src_content = subst_basedir_with_placeholder(f.read(), src_dir)
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
