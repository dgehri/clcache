from collections import namedtuple
from email.mime import base
import os
import contextlib
import json
import os
import time
from shutil import rmtree
from atomicwrites import atomic_write

from ..utils.util import *
from ..cl import CommandLineAnalyzer
from .virt import *
from .ex import *
from .config import Configuration
from .stats import Statistics
from .cache_lock import CacheLock
from .hash import *

CompilerArtifacts = namedtuple(
    "CompilerArtifacts", ["objectFilePath", "stdout", "stderr"]
)


class Manifest:
    def __init__(self, entries=None):
        if entries is None:
            entries = []
        self._entries = entries.copy()

    def entries(self):
        return self._entries

    def addEntry(self, entry):
        """Adds entry at the top of the entries"""
        self._entries.insert(0, entry)

    def touchEntry(self, objectHash):
        """Moves entry in entryIndex position to the top of entries()"""
        entryIndex = next(
            (i for i, e in enumerate(self.entries()) if e.objectHash == objectHash), 0
        )
        self._entries.insert(0, self._entries.pop(entryIndex))


# ManifestEntry: an entry in a manifest file
# `includeFiles`: list of paths to include files, which this source file uses
# `includesContentsHash`: hash of the contents of the includeFiles
# `objectHash`: hash of the object in cache
ManifestEntry = namedtuple(
    "ManifestEntry", ["includeFiles", "includesContentHash", "objectHash"]
)


class ManifestSection:
    def __init__(self, manifestSectionDir):
        self.manifestSectionDir = manifestSectionDir
        self.lock = CacheLock.forPath(self.manifestSectionDir)

    def manifestPath(self, manifestHash):
        return os.path.join(self.manifestSectionDir, f"{manifestHash}.json")

    def manifestFiles(self):
        return files_beneath(self.manifestSectionDir)

    def setManifest(self, manifestHash, manifest):
        manifestPath = self.manifestPath(manifestHash)
        trace(f"Writing manifest with manifestHash = {manifestHash} to {manifestPath}")
        ensure_dir_exists(self.manifestSectionDir)

        success = False
        for _ in range(60):
            try:
                with atomic_write(manifestPath, overwrite=True) as outFile:
                    # Converting namedtuple to JSON via OrderedDict preserves key names and keys order
                    entries = [e._asdict() for e in manifest.entries()]
                    jsonobject = {"entries": entries}
                    json.dump(jsonobject, outFile, sort_keys=True, indent=2)
                    success = True
                    break
            except Exception:
                time.sleep(1)

        if not success:
            with atomic_write(manifestPath, overwrite=True) as outFile:
                # Converting namedtuple to JSON via OrderedDict preserves key names and keys order
                entries = [e._asdict() for e in manifest.entries()]
                jsonobject = {"entries": entries}
                json.dump(jsonobject, outFile, sort_keys=True, indent=2)

    def getManifest(self, manifestHash):
        file_name = self.manifestPath(manifestHash)
        if not os.path.exists(file_name):
            return None
        try:
            with open(file_name, "r") as inFile:
                doc = json.load(inFile)
                return Manifest(
                    [
                        ManifestEntry(
                            e["includeFiles"], e["includesContentHash"], e["objectHash"]
                        )
                        for e in doc["entries"]
                    ]
                )
        except IOError:
            return None
        except ValueError:
            error(f"clcache: manifest file {file_name} was broken")
            return None


@contextlib.contextmanager
def allSectionsLocked(repository):
    sections = list(repository.sections())
    for section in sections:
        section.lock.acquire()
    try:
        yield
    finally:
        for section in sections:
            section.lock.release()


class ManifestRepository:
    # Bump this counter whenever the current manifest file format changes.
    # E.g. changing the file format from {'oldkey': ...} to {'newkey': ...} requires
    # invalidation, such that a manifest that was stored using the old format is not
    # interpreted using the new format. Instead the old file will not be touched
    # again due to a new manifest hash and is cleaned away after some time.
    MANIFEST_FILE_FORMAT_VERSION = 6

    def __init__(self, manifestsRootDir):
        self._manifestsRootDir = manifestsRootDir

    def section(self, manifestHash):
        return ManifestSection(os.path.join(self._manifestsRootDir, manifestHash[:2]))

    def sections(self):
        return (ManifestSection(path) for path in child_dirs(self._manifestsRootDir))

    def clean(self, maxManifestsSize):
        manifestFileInfos = []
        for section in self.sections():
            for filePath in section.manifestFiles():
                with contextlib.suppress(OSError):
                    manifestFileInfos.append((os.stat(filePath), filePath))
        manifestFileInfos.sort(key=lambda t: t[0].st_atime, reverse=True)

        remainingObjectsSize = 0
        for stat, filepath in manifestFileInfos:
            if remainingObjectsSize + stat.st_size <= maxManifestsSize:
                remainingObjectsSize += stat.st_size
            else:
                os.remove(filepath)
        return remainingObjectsSize

    @staticmethod
    def getManifestHash(compilerBinary, commandLine, sourceFile):
        compilerHash = getCompilerHash(compilerBinary)

        # NOTE: We intentionally do not normalize command line to include
        # preprocessor options.  In direct mode we do not perform preprocessing
        # before cache lookup, so all parameters are important.  One of the few
        # exceptions to this rule is the /MP switch, which only defines how many
        # compiler processes are running simultaneusly.  Arguments that specify
        # the compiler where to find the source files are parsed to replace
        # ocurrences of CLCACHE_BASEDIR and CLCACHE_BUILDDIR by a placeholder.
        (
            arguments,
            inputFiles,
        ) = CommandLineAnalyzer.parseArgumentsAndInputFiles(commandLine)

        def collapseBasedirInCmdPath(path):
            return collapseDirToPlaceholder(os.path.normcase(os.path.abspath(path)))

        commandLine = []
        argumentsWithPaths = ("AI", "I", "FU", "external:I", "imsvc")
        argumentsToUnifyAndSort = (
            "D",
            "MD",
            "MT",
            "W0",
            "W1",
            "W2",
            "W3",
            "W4",
            "Wall",
            "Wv",
            "WX",
            "w1",
            "w2",
            "w3",
            "w4",
            "we",
            "wo",
            "wd",
            "Z7",
            "nologo",
            "showIncludes",
        )
        for k in sorted(arguments.keys()):
            if k in argumentsWithPaths:
                commandLine.extend(
                    [f"/{k}{collapseBasedirInCmdPath(arg)}" for arg in arguments[k]]
                )
            elif k in argumentsToUnifyAndSort:
                commandLine.extend(
                    [f"/{k}{arg}" for arg in list(dict.fromkeys(arguments[k]))]
                )
            else:
                commandLine.extend([f"/{k}{arg}" for arg in arguments[k]])

        commandLine.extend(collapseBasedirInCmdPath(arg) for arg in inputFiles)

        additionalData = "{}|{}|{}".format(
            compilerHash, commandLine, ManifestRepository.MANIFEST_FILE_FORMAT_VERSION
        )
        return getFileHash(sourceFile, additionalData)

    @staticmethod
    def getIncludesContentHashForFiles(includes):
        try:
            listOfHashes = getFileHashes(includes)
        except FileNotFoundError as e:
            raise IncludeNotFoundException from e
        return ManifestRepository.getIncludesContentHashForHashes(listOfHashes)

    @staticmethod
    def getIncludesContentHashForHashes(listOfHashes):
        return HashAlgorithm(",".join(listOfHashes).encode()).hexdigest()


class CompilerArtifactsSection:
    OBJECT_FILE = "object"
    STDOUT_FILE = "output.txt"
    STDERR_FILE = "stderr.txt"

    def __init__(self, compilerArtifactsSectionDir):
        self.compilerArtifactsSectionDir = compilerArtifactsSectionDir
        self.lock = CacheLock.forPath(self.compilerArtifactsSectionDir)

    def cacheEntryDir(self, key):
        return os.path.join(self.compilerArtifactsSectionDir, key)

    def cacheEntries(self):
        return child_dirs(self.compilerArtifactsSectionDir, absolute=False)

    def cachedObjectNames(self, key):
        paths = []

        base_path = os.path.join(
            self.cacheEntryDir(key), CompilerArtifactsSection.OBJECT_FILE
        )
        if os.path.exists(base_path):
            paths.append(base_path)

        if os.path.exists(f"{base_path}.lz4"):
            paths.append(f"{base_path}.lz4")

        return paths
        
    def hasEntry(self, key):
        return os.path.exists(self.cacheEntryDir(key)), True

    def setEntry(self, key, artifacts):
        cache_entry_dir = self.cacheEntryDir(key)
        # Write new files to a temporary directory
        temp_entry_dir = f"{cache_entry_dir}.new"
        # Remove any possible left-over in tempEntryDir from previous executions
        rmtree(temp_entry_dir, ignore_errors=True)
        ensure_dir_exists(temp_entry_dir)
        if artifacts.objectFilePath is not None:
            dst_file_path = os.path.join(
                temp_entry_dir, CompilerArtifactsSection.OBJECT_FILE
            )
            size = copy_or_link(artifacts.objectFilePath, dst_file_path, True)
        set_cached_compiler_console_output(
            os.path.join(temp_entry_dir, CompilerArtifactsSection.STDOUT_FILE),
            artifacts.stdout,
        )
        if artifacts.stderr != "":
            set_cached_compiler_console_output(
                os.path.join(temp_entry_dir, CompilerArtifactsSection.STDERR_FILE),
                artifacts.stderr,
                True,
            )
        # Replace the full cache entry atomically
        os.replace(temp_entry_dir, cache_entry_dir)
        return size

    def setEntryFromPayload(self, key, payload):
        cache_entry_dir = self.cacheEntryDir(key)
        # Write new files to a temporary directory
        temp_entry_dir = f"{cache_entry_dir}.new"
        # Remove any possible left-over in tempEntryDir from previous executions
        rmtree(temp_entry_dir, ignore_errors=True)
        ensure_dir_exists(temp_entry_dir)

        if "obj" in payload:
            obj_path = os.path.join(
                temp_entry_dir, f"{CompilerArtifactsSection.OBJECT_FILE}.lz4"
            )
            with open(obj_path, "wb") as f:
                f.write(payload["obj"])
            size = os.path.getsize(obj_path)

        if "stdout" in payload:
            set_cached_compiler_console_output(
                os.path.join(temp_entry_dir, CompilerArtifactsSection.STDOUT_FILE),
                payload["stdout"],
            )

        if "stderr" in payload:
            set_cached_compiler_console_output(
                os.path.join(temp_entry_dir, CompilerArtifactsSection.STDERR_FILE),
                payload["stderr"],
                True,
            )
        # Replace the full cache entry atomically
        os.replace(temp_entry_dir, cache_entry_dir)
        return size

    def getEntry(self, key):
        hit, _ = self.hasEntry(key)
        assert hit
        cache_entry_dir = self.cacheEntryDir(key)
        return CompilerArtifacts(
            os.path.join(cache_entry_dir, CompilerArtifactsSection.OBJECT_FILE),
            get_cached_compiler_console_output(
                os.path.join(cache_entry_dir, CompilerArtifactsSection.STDOUT_FILE)
            ),
            get_cached_compiler_console_output(
                os.path.join(cache_entry_dir, CompilerArtifactsSection.STDERR_FILE),
                True,
            ),
        )


class CompilerArtifactsRepository:
    def __init__(self, compilerArtifactsRootDir):
        self._compilerArtifactsRootDir = compilerArtifactsRootDir

    def section(self, key):
        return CompilerArtifactsSection(
            os.path.join(self._compilerArtifactsRootDir, key[:2])
        )

    def sections(self):
        return (
            CompilerArtifactsSection(path)
            for path in child_dirs(self._compilerArtifactsRootDir)
        )

    def removeEntry(self, keyToBeRemoved):
        compilerArtifactsDir = self.section(keyToBeRemoved).cacheEntryDir(
            keyToBeRemoved
        )
        rmtree(compilerArtifactsDir, ignore_errors=True)

    def clean(self, maxCompilerArtifactsSize):
        objectInfos = []
        for section in self.sections():
            for cachekey in section.cacheEntries():
                with contextlib.suppress(OSError):
                    if object_file_paths := section.cachedObjectNames(cachekey):
                        object_stats = [os.stat(x) for x in object_file_paths]
                        atime = min(x.st_atime for x in object_stats)
                        size = sum(x.st_size for x in object_stats)
                        objectInfos.append((atime, size, cachekey))
        objectInfos.sort(key=lambda t: t[0])

        # compute real current size to fix up the stored cacheSize
        currentSizeObjects = sum(x[1] for x in objectInfos)

        removedItems = 0
        for atime, size, cachekey in objectInfos:
            self.removeEntry(cachekey)
            removedItems += 1
            currentSizeObjects -= size
            if currentSizeObjects < maxCompilerArtifactsSize:
                break

        return len(objectInfos) - removedItems, currentSizeObjects

    @staticmethod
    def compute_key(manifestHash, includesContentHash):
        # We must take into account manifestHash to avoid
        # collisions when different source files use the same
        # set of includes.
        return getStringHash(manifestHash + includesContentHash)

    @staticmethod
    def _normalizedCommandLine(cmdline):
        trace("_normalizedCommandLine")
        # Remove all arguments from the command line which only influence the
        # preprocessor; the preprocessor's output is already included into the
        # hash sum so we don't have to care about these switches in the
        # command line as well.
        argsToStrip = (
            "AI",
            "C",
            "E",
            "P",
            "FI",
            "u",
            "X",
            "FU",
            "D",
            "EP",
            "Fx",
            "U",
            "I",
            "external",
            "imsvc",
        )

        # Also remove the switch for specifying the output file name; we don't
        # want two invocations which are identical except for the output file
        # name to be treated differently.
        argsToStrip += ("Fo",)

        # Also strip the switch for specifying the number of parallel compiler
        # processes to use (when specifying multiple source files on the
        # command line).
        argsToStrip += ("MP",)

        result = [
            arg
            for arg in cmdline
            if not (arg[0] in "/-" and arg[1:].startswith(argsToStrip))
        ]
        trace(f"Arguments (normalized) '{result}'")
        return result


class CacheFileStrategy:
    def __init__(self, cacheDirectory=None):
        self.dir = cacheDirectory
        if not self.dir:
            try:
                self.dir = os.environ["CLCACHE_DIR"]
            except KeyError:
                self.dir = os.path.join(os.path.expanduser("~"), "clcache")

        manifestsRootDir = os.path.join(self.dir, "manifests")
        ensure_dir_exists(manifestsRootDir)
        self.manifestRepository = ManifestRepository(manifestsRootDir)

        compilerArtifactsRootDir = os.path.join(self.dir, "objects")
        ensure_dir_exists(compilerArtifactsRootDir)
        self.compilerArtifactsRepository = CompilerArtifactsRepository(
            compilerArtifactsRootDir
        )

        self.configuration = Configuration(os.path.join(self.dir, "config.txt"))
        self.statistics = Statistics(os.path.join(self.dir, "stats.txt"))

    @property
    @contextlib.contextmanager
    def lock(self):
        with allSectionsLocked(self.manifestRepository), allSectionsLocked(
            self.compilerArtifactsRepository
        ), self.statistics.lock:
            yield

    def lockFor(self, key):
        # assert isinstance(self.compilerArtifactsRepository.section(key).lock, CacheLock)
        return self.compilerArtifactsRepository.section(key).lock

    def __str__(self):
        return f"Disk cache at {self.dir}"

    def manifestLockFor(self, key):
        return self.manifestRepository.section(key).lock

    def getEntry(self, key):
        return self.compilerArtifactsRepository.section(key).getEntry(key)

    def setEntry(self, key, value):
        return self.compilerArtifactsRepository.section(key).setEntry(key, value)

    def setEntryFromPayload(self, key, payload):
        return self.compilerArtifactsRepository.section(key).setEntryFromPayload(
            key, payload
        )

    def directoryForCache(self, key):
        return self.compilerArtifactsRepository.section(key).cacheEntryDir(key)

    def hasEntry(self, cachekey):
        return self.compilerArtifactsRepository.section(cachekey).hasEntry(cachekey)

    def setManifest(self, manifestHash, manifest):
        self.manifestRepository.section(manifestHash).setManifest(
            manifestHash, manifest
        )

    def getManifest(self, manifestHash):
        return self.manifestRepository.section(manifestHash).getManifest(manifestHash)

    def clean(self, stats, maximumSize):
        currentSize = stats.currentCacheSize()
        if currentSize < maximumSize:
            return

        # Free at least 10% to avoid cleaning up too often which
        # is a big performance hit with large caches.
        effectiveMaximumSizeOverall = maximumSize * 0.9

        # Split limit in manifests (10 %) and objects (90 %)
        effectiveMaximumSizeManifests = effectiveMaximumSizeOverall * 0.1
        effectiveMaximumSizeObjects = (
            effectiveMaximumSizeOverall - effectiveMaximumSizeManifests
        )

        # Clean manifests
        currentSizeManifests = self.manifestRepository.clean(
            effectiveMaximumSizeManifests
        )

        # Clean artifacts
        (
            currentCompilerArtifactsCount,
            currentCompilerArtifactsSize,
        ) = self.compilerArtifactsRepository.clean(effectiveMaximumSizeObjects)

        stats.setCacheSize(currentCompilerArtifactsSize + currentSizeManifests)
        stats.setNumCacheEntries(currentCompilerArtifactsCount)
