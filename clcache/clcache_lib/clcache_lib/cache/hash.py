from ast import Dict
import hashlib
import os
import errno
import pickle
from ctypes import windll, wintypes
from typing import Dict

from ..config import CACHE_VERSION
from ..utils import trace
from .virt import substituteIncludeBaseDirPlaceholder

HashAlgorithm = hashlib.md5

# Define some Win32 API constants here to avoid dependency on win32pipe
NMPWAIT_WAIT_FOREVER = wintypes.DWORD(0xFFFFFFFF)
ERROR_PIPE_BUSY = 231

def getCompilerHash(compilerBinary):
    stat = os.stat(compilerBinary)
    data = "|".join(
        [
            str(stat.st_mtime),
            str(stat.st_size),
            CACHE_VERSION,
        ]
    )
    hasher = HashAlgorithm()
    hasher.update(data.encode("UTF-8"))
    return hasher.hexdigest()


def getFileHashes(filePaths):
    if "CLCACHE_SERVER" not in os.environ:
        return [getFileHashCached(filePath) for filePath in filePaths]

    pipeName = r"\\.\pipe\clcache_srv"
    while True:
        try:
            with open(pipeName, "w+b") as f:
                f.write("\n".join(filePaths).encode("utf-8"))
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
                windll.kernel32.WaitNamedPipeW(pipeName, NMPWAIT_WAIT_FOREVER)
            else:
                raise


knownHashes: Dict[str, str] = {}


def getFileHashCached(filePath):
    if filePath in knownHashes:
        return knownHashes[filePath]
    c = getFileHash(filePath)
    knownHashes[filePath] = c
    return c


def getFileHash(filePath, additionalData=None):
    hasher = HashAlgorithm()
    with open(filePath, "rb") as inFile:
        hasher.update(substituteIncludeBaseDirPlaceholder(inFile.read()))

    # trace(f"File hash: {filePath} => {hasher.hexdigest()}", 2)

    if additionalData is not None:
        # Encoding of this additional data does not really matter
        # as long as we keep it fixed, otherwise hashes change.
        # The string should fit into ASCII, so UTF8 should not change anything
        hasher.update(additionalData.encode("UTF-8"))
        # trace(f"AdditionalData Hash: {hasher.hexdigest()}: {additionalData}", 2)

    return hasher.hexdigest()


def getStringHash(dataString):
    hasher = HashAlgorithm()
    hasher.update(dataString.encode("UTF-8"))
    return hasher.hexdigest()
