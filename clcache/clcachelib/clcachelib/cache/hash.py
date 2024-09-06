from collections import defaultdict, deque
from enum import Enum
import functools
import hashlib
import os
import sys
import traceback
from pathlib import Path
import subprocess as sp
from ..config import CACHE_VERSION
from ..config.config import HASH_SERVER_TIMEOUT
from ..utils.logging import LogLevel, log
from .virt import is_in_build_dir, subst_basedir_with_placeholder

HashAlgorithm = hashlib.md5

# Not the same as VERSION !
SERVER_VERSION = "3"
UUID = rf"626763c0-bebe-11ed-a901-0800200c9a66-{SERVER_VERSION}"


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
        if server_idle_timeout_s := _get_sever_timeout_seconds():
            try:
                return _get_file_hashes_impl(path_list, server_idle_timeout_s)
            except FileNotFoundError:
                # This is expected
                raise
            except Exception as e:
                log(f"Failed to use server: {traceback.format_exc()}", LogLevel.ERROR)

    return [get_file_hash(path) for path in path_list]


def _get_file_hashes_impl(path_list, server_idle_timeout_s):
    class Location(Enum):
        CACHE = 1
        BUILD_DIR = 2
        OTHER = 3

    paths_by_location = defaultdict(deque)
    path_locations = {}

    for path in path_list:
        if path in _get_file_hashes_impl.cache:
            path_locations[path] = Location.CACHE
        elif is_in_build_dir(path):
            path_locations[path] = Location.BUILD_DIR
            paths_by_location[Location.BUILD_DIR].append(path)
        else:
            path_locations[path] = Location.OTHER
            paths_by_location[Location.OTHER].append(path)

    # Fetch hashes based on location
    other_hashes = _get_file_hashes_from_server(
        paths_by_location[Location.OTHER], server_idle_timeout_s
    )
    build_dir_hashes = deque(
        [get_file_hash(path) for path in paths_by_location[Location.BUILD_DIR]]
    )

    hashes = []

    for path in path_list:
        if path_locations[path] == Location.CACHE:
            hashes.append(_get_file_hashes_impl.cache[path])
        elif path_locations[path] == Location.BUILD_DIR:
            hashes.append(build_dir_hashes.popleft())
        else:
            hashes.append(other_hashes.popleft())

    return hashes


_get_file_hashes_impl.cache = {}


def _get_file_hashes_from_server(
    path_list: deque[Path], server_idle_timeout_s: int
) -> deque[str]:
    """Get file hashes from clcache server."""

    # Return empty list if no paths
    if not path_list:
        return deque()

    # Launch clcache_server.exe located in the same directory as this executable
    args = _construct_server_args(server_idle_timeout_s)

    with sp.Popen(
        args,
        creationflags=sp.CREATE_NO_WINDOW,
        stdin=sp.PIPE,
        stdout=sp.PIPE,
        stderr=sp.PIPE,
    ) as p:
        # Write paths to stdin (separated by newline, and a final empty line)
        stdin_data = "\n".join(map(str, path_list)) + "\n\n"

        # Wait for process to finish and get output
        stdout, stderr = p.communicate(
            input=stdin_data.encode("utf-8"), timeout=server_idle_timeout_s
        )
        if p.returncode != 0:
            raise RuntimeError(f"Process exited with code {p.returncode}")

    # Read result from stdout
    response = stdout.decode("utf-8")

    if response.startswith("!"):
        # extract error string
        raise FileNotFoundError(response[1:-1])

    return deque(response[:-1].splitlines())


def _construct_server_args(server_idle_timeout_s: int) -> list[str]:
    """Construct arguments for server process."""
    base_path = (
        Path(sys.argv[0]).absolute().parent
        if "__compiled__" not in globals()
        else Path(sys.executable).parent.parent
    )
    server_exe = base_path / (
        "clcache_server/target/release/clcache_server.exe"
        if "__compiled__" not in globals()
        else "clcache_server.exe"
    )

    return [
        str(server_exe),
        f"--idle-timeout={server_idle_timeout_s}",
        f"--id={UUID}",
        "--client-mode",
    ]


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
