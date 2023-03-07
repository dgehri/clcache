import os
import contextlib
import json
import os
import time
from typing import NamedTuple
from atomicwrites import atomic_write

from ..utils.util import *
from ..cl import CommandLineAnalyzer
from .virt import *
from .ex import *
from .config import Configuration
from .stats import PersistentStats, Stats
from .cache_lock import CacheLock
from .hash import *


class CompilerArtifacts(NamedTuple):
    '''
    Represents a set of artifacts produced by a compiler invocation

        - obj_file_path: path to the object file
        - stdout: stdout of the compiler
        - stderr: stderr of the compiler
    '''
    obj_file_path: Path
    stdout: str
    stderr: str


class ManifestEntry(NamedTuple):
    '''
    An entry in a manifest file

        - includeFiles: list of paths to include files, which this source file uses
        - includesContentHash: hash of the contents of the include_files
        - objectHash: hash of the object in cache
    '''
    includeFiles: List[str]
    includesContentHash: str
    objectHash: str


class Manifest:
    '''Represents a manifest file'''

    def __init__(self, entries: Optional[List[ManifestEntry]] = None):
        if entries is None:
            entries = []
        self._entries = entries.copy()

    def entries(self):
        return self._entries

    def add_entry(self, entry: ManifestEntry):
        """Adds entry at the top of the entries"""
        self._entries.insert(0, entry)

    def touch_entry(self, obj_hash: str):
        """Moves entry in entry_index position to the top of entries()"""
        entry_index = next(
            (i for i, e in enumerate(self.entries())
             if e.objectHash == obj_hash), 0
        )
        self._entries.insert(0, self._entries.pop(entry_index))


class ManifestSection:
    def __init__(self, manifestSectionDir: Path):
        self.manifestSectionDir: Path = manifestSectionDir
        self.lock = CacheLock.for_path(self.manifestSectionDir)

    def manifest_path(self, manifestHash: str) -> Path:
        return self.manifestSectionDir / f"{manifestHash}.json"

    def manifest_files(self) -> Generator[Path, None, None]:
        return files_beneath(self.manifestSectionDir)

    def set_manifest(self, manifestHash: str, manifest: Manifest) -> int:
        '''Writes manifest to disk and returns the size of the manifest file'''
        manifest_path = self.manifest_path(manifestHash)
        trace(
            f"Writing manifest with manifestHash = {manifestHash} to {manifest_path}")
        ensure_dir_exists(self.manifestSectionDir)

        # Retry writing manifest file in case of concurrent access
        for i in range(60):
            try:
                with atomic_write(manifest_path, overwrite=True) as outFile:
                    # Converting namedtuple to JSON via OrderedDict preserves key names and keys order
                    entries = [e._asdict() for e in manifest.entries()]
                    jsonobject = {"entries": entries}
                    json.dump(jsonobject, outFile, sort_keys=True, indent=2)
                    return manifest_path.stat().st_size
            except Exception:
                if i == 59:
                    raise
                time.sleep(1)
        assert False, "unreachable"

    def get_manifest(self, manifest_hash: str) -> Optional[Tuple[Manifest, int]]:
        '''Reads manifest from disk and returns the size of the manifest file'''
        manifest_file = self.manifest_path(manifest_hash)
        if not manifest_file.exists():
            return None
        try:
            with open(manifest_file, "r") as in_file:
                doc = json.load(in_file)
                return Manifest(
                    [
                        ManifestEntry(
                            e["includeFiles"],
                            e["includesContentHash"],
                            e["objectHash"]
                        )
                        for e in doc["entries"]
                    ]
                ), manifest_file.stat().st_size
        except IOError:
            return None
        except (ValueError, KeyError):
            error(f"clcache: manifest file {manifest_file} was broken")
            return None


@contextlib.contextmanager
def all_sections_locked(repository):
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

    def __init__(self, manifestsRootDir: Path):
        self._manifestsRootDir: Path = manifestsRootDir

    def section(self, manifestHash: str) -> ManifestSection:
        return ManifestSection(self._manifestsRootDir / manifestHash[:2])

    def sections(self) -> Generator[ManifestSection, None, None]:
        return (ManifestSection(path) for path in child_dirs(self._manifestsRootDir))

    def clean(self, maxManifestsSize: int) -> int:
        '''
        Removes old manifest files until the total size of the remaining manifest files is less than maxManifestsSize.

        Parameters:
            maxManifestsSize: The maximum size of the remaining manifest files in bytes.

        Returns:
            The total size of the remaining manifest files in bytes.
        '''
        manifestFileInfos = []
        for section in self.sections():
            for filePath in section.manifest_files():
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
    def get_manifest_hash(compiler_path: Path, cmd_line: List[str], src_file: Path) -> str:
        '''
        Returns a hash of the manifest file that would be used for the given command line.
        '''
        compiler_hash = get_compiler_hash(compiler_path)

        # NOTE: We intentionally do not normalize command line to include
        # preprocessor options.  In direct mode we do not perform preprocessing
        # before cache lookup, so all parameters are important.  One of the few
        # exceptions to this rule is the /MP switch, which only defines how many
        # compiler processes are running simultaneusly.  Arguments that specify
        # the compiler where to find the source files are parsed to replace
        # ocurrences of CLCACHE_BASEDIR and CLCACHE_BUILDDIR by a placeholder.
        (
            args,
            input_files,
        ) = CommandLineAnalyzer.parse_args_and_input_files(cmd_line)

        def canonicalize_path_arg(arg: Path):
            return canonicalize_path(arg.absolute())

        cmd_line = []
        args_with_paths = ("AI", "I", "FU", "external:I", "imsvc")
        args_to_unify_and_sort = (
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
        for k in sorted(args.keys()):
            if k in args_with_paths:
                cmd_line.extend(
                    [f"/{k}{canonicalize_path_arg(Path(arg))}" for arg in args[k]]
                )
            elif k in args_to_unify_and_sort:
                cmd_line.extend(
                    [f"/{k}{arg}" for arg in list(dict.fromkeys(args[k]))]
                )
            else:
                cmd_line.extend([f"/{k}{arg}" for arg in args[k]])

        cmd_line.extend(canonicalize_path_arg(arg) for arg in input_files)

        toolset_data = "{}|{}|{}".format(
            compiler_hash, cmd_line, ManifestRepository.MANIFEST_FILE_FORMAT_VERSION
        )
        return get_file_hash(src_file, toolset_data)

    @staticmethod
    def get_includes_content_hash_for_files(includes: List[Path]) -> str:
        try:
            list_of_hashes = get_file_hashes(includes)
        except FileNotFoundError as e:
            raise IncludeNotFoundException from e
        return ManifestRepository.get_includes_content_hash_for_hashes(list_of_hashes)

    @staticmethod
    def get_includes_content_hash_for_hashes(hashes: List[str]) -> str:
        return HashAlgorithm(",".join(hashes).encode()).hexdigest()


class CompilerArtifactsSection:
    OBJECT_FILE: str = "object"
    STDOUT_FILE: str = "output.txt"
    STDERR_FILE: str = "stderr.txt"

    def __init__(self, compiler_artifacts_section_dir: Path):
        self.compilerArtifactsSectionDir: Path = compiler_artifacts_section_dir
        self.lock = CacheLock.for_path(self.compilerArtifactsSectionDir)

    def cache_entry_dir(self, key: str) -> Path:
        '''Returns the path to the cache entry directory for the given key.'''
        return self.compilerArtifactsSectionDir / key

    def cache_entries(self) -> Generator[str, None, None]:
        '''Returns a generator of cache entry keys.'''
        return child_dirs_str(str(self.compilerArtifactsSectionDir), absolute=False)

    def cached_objects(self, key: str) -> List[Path]:
        '''Returns a list of paths to cached object files for the given key.'''
        paths: List[Path] = []

        base_path = self.cache_entry_dir(
            key) / CompilerArtifactsSection.OBJECT_FILE

        if base_path.exists():
            paths.append(base_path)

        compressed_path = base_path.parent / f"{base_path.name}.lz4"
        if compressed_path.exists():
            paths.append(compressed_path)

        return paths

    def has_entry(self, key: str) -> Tuple[bool, bool]:
        '''
        Test if the cache entry for the given key exists.

        Returns:
            A tuple of (has entry, is local cache entry).
        '''
        return self.cache_entry_dir(key).exists(), True

    def set_entry(self, key: str, artifacts: CompilerArtifacts) -> int:
        '''
        Sets the cache entry for the given key to the given artifacts.

        Returns:
            The size of the cache entry in bytes.
        '''
        cache_entry_dir = self.cache_entry_dir(key)
        # Write new files to a temporary directory
        temp_entry_dir: Path = cache_entry_dir.parent / \
            f"{cache_entry_dir.name}.new"

        remove_and_recreate_dir(temp_entry_dir)
        size = 0

        # Write the object file to file
        if artifacts.obj_file_path is not None:
            dst_file_path = temp_entry_dir / CompilerArtifactsSection.OBJECT_FILE
            size = copy_to_cache(artifacts.obj_file_path, dst_file_path)

        # Write the stdout to file
        set_cached_compiler_console_output(
            temp_entry_dir / CompilerArtifactsSection.STDOUT_FILE,
            artifacts.stdout,
        )
        size += len(artifacts.stdout)

        # Write the stderr to file
        if artifacts.stderr != "":
            set_cached_compiler_console_output(temp_entry_dir / CompilerArtifactsSection.STDERR_FILE,
                                               artifacts.stderr,
                                               True,
                                               )
            size += len(artifacts.stderr)

        # Replace the full cache entry atomically
        os.replace(temp_entry_dir, cache_entry_dir)
        return size

    def set_entry_from_payload(self, key: str, payload: dict) -> int:
        '''
        Sets the cache entry for the given key to the given artifacts.

        Parameters:
            key: The key of the cache entry to set.
            payload: A dictionary containing the artifacts to set.

        Returns:
            The size of the cache entry in bytes.
        '''
        cache_entry_dir = self.cache_entry_dir(key)
        # Write new files to a temporary directory
        temp_entry_dir = Path(f"{cache_entry_dir}.new")
        remove_and_recreate_dir(temp_entry_dir)

        size = 0
        if "obj" in payload:
            obj_path = os.path.join(
                temp_entry_dir, f"{CompilerArtifactsSection.OBJECT_FILE}.lz4"
            )
            with open(obj_path, "wb") as f:
                f.write(payload["obj"])
            size = os.path.getsize(obj_path)

        if "stdout" in payload:
            set_cached_compiler_console_output(
                temp_entry_dir / CompilerArtifactsSection.STDOUT_FILE,
                payload["stdout"],
                False
            )

        if "stderr" in payload:
            set_cached_compiler_console_output(
                temp_entry_dir / CompilerArtifactsSection.STDERR_FILE,
                payload["stderr"],
                True
            )
        # Replace the full cache entry atomically
        os.replace(temp_entry_dir, cache_entry_dir)
        return size

    def get_entry(self, key: str) -> CompilerArtifacts:
        hit, _ = self.has_entry(key)
        assert hit
        cache_entry_dir = self.cache_entry_dir(key)
        return CompilerArtifacts(
            cache_entry_dir / CompilerArtifactsSection.OBJECT_FILE,
            get_cached_compiler_console_output(
                cache_entry_dir / CompilerArtifactsSection.STDOUT_FILE),
            get_cached_compiler_console_output(
                cache_entry_dir / CompilerArtifactsSection.STDERR_FILE, True)
        )


class CompilerArtifactsRepository:
    '''A repository for compiler artifacts.'''

    def __init__(self, compilerArtifactsRootDir: Path):
        self._compilerArtifactsRootDir = compilerArtifactsRootDir

    def section(self, key: str):
        return CompilerArtifactsSection(self._compilerArtifactsRootDir / key[:2])

    def sections(self):
        return (
            CompilerArtifactsSection(path)
            for path in child_dirs(self._compilerArtifactsRootDir)
        )

    def removeEntry(self, keyToBeRemoved):
        compilerArtifactsDir = self.section(keyToBeRemoved).cache_entry_dir(
            keyToBeRemoved
        )
        rmtree(compilerArtifactsDir, ignore_errors=True)

    def clean(self, max_compiler_artifacts_size: int) -> Tuple[int, int]:
        '''
        Removes compiler artifacts until the total size of the artifacts is less than the given maximum size.

        Parameters:
            max_compiler_artifacts_size: The maximum size of the artifacts in bytes.

        Returns:
            A tuple of (number of artifacts removed, total size of artifacts in bytes).
        '''
        object_infos = []
        for section in self.sections():
            for cachekey in section.cache_entries():
                with contextlib.suppress(OSError):
                    if object_file_paths := section.cached_objects(cachekey):
                        object_stats = [os.stat(x) for x in object_file_paths]
                        atime = min(x.st_atime for x in object_stats)
                        size = sum(x.st_size for x in object_stats)
                        object_infos.append((atime, size, cachekey))
        object_infos.sort(key=lambda t: t[0])

        # compute real current size to fix up the stored cacheSize
        current_size_objs = sum(x[1] for x in object_infos)

        removed_items = 0
        for atime, size, cachekey in object_infos:
            self.removeEntry(cachekey)
            removed_items += 1
            current_size_objs -= size
            if current_size_objs < max_compiler_artifacts_size:
                break

        return len(object_infos) - removed_items, current_size_objs

    @staticmethod
    def compute_key(manifest_hash: str, includes_content_hash: str) -> str:
        '''
        Computes the key for the given manifest hash and includes content hash.

        Parameters:
            manifest_hash: The hash of the manifest.
            includes_content_hash: The hash of the includes content.

        Returns:
            The key for the given manifest hash and includes content hash.
        '''
        # We must take into account manifest_hash to avoid
        # collisions when different source files use the same
        # set of includes.
        return get_string_hash(manifest_hash + includes_content_hash)


class CacheFileStrategy:
    def __init__(self, cache_dir: Optional[Path] = None):
        self.dir = cache_dir
        if not self.dir:
            try:
                self.dir = Path(os.environ["CLCACHE_DIR"])
            except KeyError:
                self.dir = Path.home() / "clcache"

        manifests_root_dir = self.dir / "manifests"
        ensure_dir_exists(manifests_root_dir)
        self.manifests_repository = ManifestRepository(manifests_root_dir)

        compiler_artifacts_root_dir = self.dir / "objects"
        ensure_dir_exists(compiler_artifacts_root_dir)
        self.compiler_artifacts_repository = CompilerArtifactsRepository(
            compiler_artifacts_root_dir
        )

        self.configuration = Configuration(self.dir / "config.txt")
        self.persistent_stats = PersistentStats(self.dir / "stats.txt")
        self.current_stats = Stats()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.persistent_stats.save_combined(self.current_stats)

        # also save the current stats to the build directory
        build_stats = PersistentStats(Path(BUILDDIR_STR) / "clcache.json")
        build_stats.save_combined(self.current_stats)

    @property
    @contextlib.contextmanager
    def lock(self):
        with all_sections_locked(self.manifests_repository), all_sections_locked(
            self.compiler_artifacts_repository
        ):
            yield

    def lock_for(self, key: str):
        # assert isinstance(self.compilerArtifactsRepository.section(key).lock, CacheLock)
        return self.compiler_artifacts_repository.section(key).lock

    def __str__(self):
        return f"disk cache at {self.dir}"

    def manifest_lock_for(self, key: str) -> CacheLock:
        '''Get the lock for the given key or raise a KeyError if the entry does not exist.'''
        return self.manifests_repository.section(key).lock

    def get_entry(self, key: str) -> CompilerArtifacts:
        '''Get the entry for the given key or raise a KeyError if the entry does not exist.'''
        return self.compiler_artifacts_repository.section(key).get_entry(key)

    def set_entry(self, key: str, value) -> int:
        '''Set the entry for the given key to the given value and return the size of the entry in bytes.'''
        return self.compiler_artifacts_repository.section(key).set_entry(key, value)

    def set_entry_from_payload(self, key: str, payload: dict) -> int:
        '''Set the entry for the given key to the given value and return the size of the entry in bytes.'''
        return self.compiler_artifacts_repository.section(key).set_entry_from_payload(
            key, payload
        )

    def get_directory_for_key(self, key: str) -> Path:
        '''Get the directory for the given key or raise a KeyError if the entry does not exist.'''
        return self.compiler_artifacts_repository.section(key).cache_entry_dir(key)

    def has_entry(self, cachekey: str) -> Tuple[bool, bool]:
        '''
        Determines whether the cache contains an entry for the given key.

        Returns:
            A tuple of (has entry, is local cache entry).
        '''
        return self.compiler_artifacts_repository.section(cachekey).has_entry(cachekey)

    def set_manifest(self, manifest_hash: str, manifest: Manifest) -> int:
        return self.manifests_repository.section(manifest_hash).set_manifest(
            manifest_hash, manifest
        )

    def get_manifest(self, manifest_hash: str) -> Optional[Tuple[Manifest, int]]:
        return self.manifests_repository.section(manifest_hash).get_manifest(manifest_hash)

    def clear(self):
        self._clean(0)

    def clean(self):
        self._clean(self.configuration.max_cache_size())

    def _clean(self, max_size: int):
        cur_size = self.persistent_stats.cache_size() + self.current_stats.cache_size()
        if cur_size < max_size:
            return

        # Free at least 10% to avoid cleaning up too often which
        # is a big performance hit with large caches.
        max_overall_size = max_size * 0.9

        # Split limit in manifests (10 %) and objects (90 %)
        max_manifests_size = max_overall_size * 0.1
        max_objects_size = (max_overall_size - max_manifests_size)

        # Clean manifests
        new_manif_size = self.manifests_repository.clean(
            int(max_manifests_size))

        # Clean artifacts
        (new_count, new_size,) = self.compiler_artifacts_repository.clean(
            int(max_objects_size))

        self.persistent_stats.set_cache_size_and_entries(
            new_size + new_manif_size, new_count)
        self.current_stats.clear_cache_size()
        self.current_stats.clear_cache_entries()
