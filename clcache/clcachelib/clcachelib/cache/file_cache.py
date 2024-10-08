import contextlib
import json
import os
import time
from collections.abc import Callable
from shutil import copyfileobj
from typing import BinaryIO, NamedTuple, cast

import lz4.frame
from atomicwrites import atomic_write

from ..utils.file_lock import FileLock
from ..utils.logging import LogLevel, log
from ..utils.util import *
from .config import Configuration
from .ex import *
from .hash import *
from .stats import PersistentStats, Stats
from .virt import *


class CompilerArtifacts(NamedTuple):
    '''
    Represents a set of artifacts produced by a compiler invocation

        - obj_file_path: path to the object file
        - stdout: stdout of the compiler
        - stderr: stderr of the compiler
    '''
    payload_path: Path
    stdout: str
    stderr: str
    copy_filter: Callable[[BinaryIO, BinaryIO], None] | None = None


class ManifestEntry(NamedTuple):
    '''
    An entry in a manifest file

        - includeFiles: list of paths to include files, which this source file uses
        - includesContentHash: hash of the contents of the include_files
        - objectHash: hash calculated from includeContentHash and the manifest hash
    '''
    includeFiles: list[str]
    includesContentHash: str
    objectHash: str

    def __hash__(self):
        '''
        Returns the hash

        The includesContentHash is a function of the includeFiles, 
        while the objectHash is a function of the manifest hash and the 
        includesContentHash. Therefore, for a given manifest file, the 
        includesContentHash uniquely identifies the entry.
        '''
        return hash(self.includesContentHash)


class Manifest:
    '''Represents a manifest file'''

    def __init__(self, entries: list[ManifestEntry] | None = None):
        if entries is None:
            entries = []
        self._entries: list[ManifestEntry] = entries.copy()

    def entries(self) -> list[ManifestEntry]:
        return self._entries

    def add_entry(self, entry: ManifestEntry):
        """Adds entry at the top of the entries"""
        # Remove existing entry with the same includeHash
        self._entries = [
            e for e in self._entries if e.includesContentHash != entry.includesContentHash]
        self._entries.insert(0, entry)

    def touch_entry(self, obj_hash: str):
        """Moves entry in entry_index position to the top of entries()"""
        entry_index = next(
            (i for i, e in enumerate(self.entries())
             if e.objectHash == obj_hash), 0
        )
        self._entries.insert(0, self._entries.pop(entry_index))


class ManifestSection:
    def __init__(self, manifest_section_dir: Path):
        self.manifestSectionDir: Path = manifest_section_dir
        self.lock = FileLock.for_path(self.manifestSectionDir)

    def manifest_path(self, manifest_hash: str) -> Path:
        return self.manifestSectionDir / f"{manifest_hash}.json"

    def manifest_files(self) -> Generator[Path, None, None]:
        return files_beneath(self.manifestSectionDir)

    def set_manifest(self, manifest_hash: str, manifest: Manifest) -> int:
        '''Writes manifest to disk and returns the size of the manifest file'''
        manifest_path = self.manifest_path(manifest_hash)
        log(f"Writing manifest {manifest_hash} to local {manifest_path}")
        ensure_dir_exists(self.manifestSectionDir)

        # Retry writing manifest file in case of concurrent
        # access (TODO: verify if this is still needed)
        for i in range(10):
            try:
                with atomic_write(manifest_path, overwrite=True) as out_file:
                    # Converting namedtuple to JSON via OrderedDict preserves key names and keys order
                    entries = [e._asdict() for e in manifest.entries()]
                    jsonobject = {"entries": entries}
                    json.dump(jsonobject, out_file, sort_keys=True, indent=2)

                # Return the size of the manifest file (warning: don't move inside the with block!)
                return manifest_path.stat().st_size
            except Exception:
                if i == 9:
                    log(
                        f"Failed to write manifest file {manifest_path}: {traceback.format_exc()}", LogLevel.ERROR)
                    raise
                log(
                    f"Failed to write manifest file {manifest_path}: {traceback.format_exc()} (retrying)", LogLevel.WARN)
                time.sleep(0.5)
        assert False, "unreachable"

    def get_manifest(self, manifest_hash: str) -> tuple[Manifest, int] | None:
        '''Reads manifest from disk and returns the size of the manifest file'''
        manifest_file = self.manifest_path(manifest_hash)
        if not manifest_file.exists():
            return None
        try:
            # touch manifest file to prevent it from being cleaned up
            manifest_file.touch()

            with open(manifest_file) as in_file:
                doc = json.load(in_file)
                visited = set()
                return Manifest(
                    [
                        ManifestEntry(
                            e["includeFiles"],
                            e["includesContentHash"],
                            e["objectHash"]
                        )
                        for e in doc["entries"]
                        if e["includesContentHash"] not in visited
                        and not visited.add(e["includesContentHash"])
                    ]
                ), manifest_file.stat().st_size
        except OSError:
            return None
        except (ValueError, KeyError):
            log(
                f"Manifest file {manifest_file} was broken", LogLevel.ERROR)
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
    MANIFEST_FILE_CL_FORMAT_VERSION = 6
    MANIFEST_FILE_MOC_FORMAT_VERSION = 8

    def __init__(self, manifest_root_dir: Path):
        self._manifestsRootDir: Path = manifest_root_dir

    def section(self, manifest_hash: str) -> ManifestSection:
        return ManifestSection(self._manifestsRootDir / manifest_hash[:2])

    def sections(self) -> Generator[ManifestSection, None, None]:
        return (ManifestSection(path) for path in child_dirs(self._manifestsRootDir))

    def clean(self, max_manifest_size: int) -> int:
        '''
        Removes old manifest files until the total size of the remaining manifest files is less than max_manifest_size.

        Parameters:
            max_manifest_size: The maximum size of the remaining manifest files in bytes.

        Returns:
            The total size of the remaining manifest files in bytes.
        '''
        manifest_file_infos: list[tuple[os.stat_result, Path]] = []
        for section in self.sections():
            file_path: Path
            for file_path in section.manifest_files():
                with contextlib.suppress(OSError):
                    manifest_file_infos.append((file_path.stat(), file_path))
        manifest_file_infos.sort(key=lambda t: t[0].st_mtime, reverse=True)

        remaining_obj_size = 0
        for stat, filepath in manifest_file_infos:
            if remaining_obj_size + stat.st_size <= max_manifest_size:
                remaining_obj_size += stat.st_size
            else:
                os.remove(filepath)
        return remaining_obj_size

    @staticmethod
    def get_includes_content_hash_for_files(includes: list[Path]) -> str:
        try:
            list_of_hashes = get_file_hashes(includes)
        except FileNotFoundError as e:
            raise IncludeNotFoundException from e
        return ManifestRepository.get_includes_content_hash_for_hashes(list_of_hashes)

    @staticmethod
    def get_includes_content_hash_for_hashes(hashes: list[str]) -> str:
        return HashAlgorithm(",".join(hashes).encode()).hexdigest()


class CompilerArtifactsSection:
    PAYLOAD_FILE: str = "object"
    STDOUT_FILE: str = "output.txt"
    STDERR_FILE: str = "stderr.txt"

    def __init__(self, compiler_artifacts_section_dir: Path):
        self.compilerArtifactsSectionDir: Path = compiler_artifacts_section_dir
        self.lock = FileLock.for_path(self.compilerArtifactsSectionDir)

    def cache_entry_dir(self, key: str) -> Path:
        '''Returns the path to the cache entry directory for the given key.'''
        return self.compilerArtifactsSectionDir / key

    def cache_entries(self) -> Generator[str, None, None]:
        '''Returns a generator of cache entry keys.'''
        return child_dirs_str(str(self.compilerArtifactsSectionDir), absolute=False)

    def cached_objects(self, key: str) -> list[Path]:
        '''Returns a list of paths to cached object files for the given key.'''
        paths: list[Path] = []

        base_path = self.cache_entry_dir(
            key) / CompilerArtifactsSection.PAYLOAD_FILE

        if base_path.exists():
            paths.append(base_path)

        compressed_path = base_path.parent / f"{base_path.name}.lz4"
        if compressed_path.exists():
            paths.append(compressed_path)

        return paths

    def has_entry(self, key: str) -> bool:
        '''
        Test if the cache entry for the given key exists.

        Returns:
            A tuple of (has entry, is local cache entry).
        '''
        entry_dir: Path = self.cache_entry_dir(key)
        return entry_dir.exists() and any(entry_dir.iterdir())

    def set_entry(self, key: str, artifacts: CompilerArtifacts) -> tuple[int, Path | None]:
        # sourcery skip: extract-method
        '''
        Sets the cache entry for the given key to the given artifacts.

        Returns:
            The size of the cache entry in bytes.
        '''
        cache_entry_dir = self.cache_entry_dir(key)
        # Write new files to a temporary directory
        temp_entry_dir: Path = cache_entry_dir.parent / \
            f"{cache_entry_dir.name}.new"

        try:
            remove_and_recreate_dir(temp_entry_dir)
            size = 0

            # Write the object file to file
            compressed_payload_path = None
            if artifacts.payload_path is not None:
                dst_file_path = temp_entry_dir / CompilerArtifactsSection.PAYLOAD_FILE
                size, obj_file_name = _copy_to_cache(
                    artifacts.payload_path, dst_file_path, artifacts.copy_filter)

                compressed_payload_path = cache_entry_dir / obj_file_name

            # Write the stdout to file
            self._write_to_cache(
                temp_entry_dir / CompilerArtifactsSection.STDOUT_FILE,
                artifacts.stdout
            )
            size += len(artifacts.stdout)

            # Write the stderr to file
            if artifacts.stderr != "":
                self._write_to_cache(temp_entry_dir / CompilerArtifactsSection.STDERR_FILE,
                                     artifacts.stderr
                                     )
                size += len(artifacts.stderr)

            # Replace the full cache entry atomically
            if cache_entry_dir.exists():
                rmtree(cache_entry_dir, ignore_errors=True)

            temp_entry_dir.replace(cache_entry_dir)

            return size, compressed_payload_path
        finally:
            # Clean up the temporary directory
            if temp_entry_dir.exists():
                rmtree(temp_entry_dir, ignore_errors=True)

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
                temp_entry_dir, f"{CompilerArtifactsSection.PAYLOAD_FILE}.lz4"
            )
            with open(obj_path, "wb") as f:
                f.write(payload["obj"])
            size = os.path.getsize(obj_path)

        if "stdout" in payload:
            self._write_to_cache(
                temp_entry_dir / CompilerArtifactsSection.STDOUT_FILE,
                payload["stdout"]
            )

        if "stderr" in payload:
            self._write_to_cache(
                temp_entry_dir / CompilerArtifactsSection.STDERR_FILE,
                payload["stderr"]
            )
        # Replace the full cache entry atomically
        os.replace(temp_entry_dir, cache_entry_dir)
        return size

    def get_entry(self, key: str) -> CompilerArtifacts:
        hit = self.has_entry(key)
        assert hit
        cache_entry_dir = self.cache_entry_dir(key)

        # "touch" the cache entry to update its last modified time
        obj_file = cache_entry_dir / CompilerArtifactsSection.PAYLOAD_FILE
        obj_file.touch()

        return CompilerArtifacts(
            obj_file,
            self._read_from_cache(
                cache_entry_dir / CompilerArtifactsSection.STDOUT_FILE),
            self._read_from_cache(
                cache_entry_dir / CompilerArtifactsSection.STDERR_FILE)
        )

    @staticmethod
    def _write_to_cache(path: Path, output: str):
        with open(path, "wb") as f:
            f.write(output.encode())

    @staticmethod
    def _read_from_cache(path: Path) -> str:
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return ""


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

    def remove_entry(self, key: str):
        compilerArtifactsDir = self.section(key).cache_entry_dir(
            key
        )
        rmtree(compilerArtifactsDir, ignore_errors=True)

    def clean(self, max_compiler_artifacts_size: int) -> tuple[int, int]:
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
                        mtime = min(x.st_mtime for x in object_stats)
                        size = sum(x.st_size for x in object_stats)
                        object_infos.append((mtime, size, cachekey))
        object_infos.sort(key=lambda t: t[0])

        # compute real current size to fix up the stored cacheSize
        current_size_objs = sum(x[1] for x in object_infos)

        removed_items = 0
        for mtime, size, cachekey in object_infos:
            self.remove_entry(cachekey)
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
    def __init__(self, cache_dir: Path | None = None):
        self.dir = cache_dir
        if not self.dir:
            try:
                env_var_name = "CLCACHE_DIR"
                self.dir = Path(os.environ[env_var_name])
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
        with contextlib.suppress(Exception):
            build_stats = PersistentStats(
                Path(BUILDDIR_STR) / f"{get_program_name()}.json")
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
        assert self.dir is not None
        return f"disk cache at {self.dir}"

    def manifest_lock_for(self, key: str) -> FileLock:
        '''Get the lock for the given key or raise a KeyError if the entry does not exist.'''
        return self.manifests_repository.section(key).lock

    def get_entry(self, key: str) -> CompilerArtifacts:
        '''Get the entry for the given key or raise a KeyError if the entry does not exist.'''
        return self.compiler_artifacts_repository.section(key).get_entry(key)

    def set_entry(self, key: str, value) -> int:
        '''Set the entry for the given key to the given value and return the size of the entry in bytes.'''
        size, _ = self.set_entry_ex(key, value)
        return size

    def set_entry_ex(self, key: str, value) -> tuple[int, Path | None]:
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

    def has_entry(self, cachekey: str) -> bool:
        '''
        Determines whether the cache contains an entry for the given key.

        Returns:
            A tuple of (has entry, is local cache entry).
        '''
        return self.compiler_artifacts_repository.section(cachekey).has_entry(cachekey)

    def set_manifest(self, manifest_hash: str, manifest: Manifest, _) -> int:
        return self.manifests_repository.section(manifest_hash).set_manifest(
            manifest_hash, manifest
        )

    def get_manifest(self, manifest_hash: str, _: bool = True) -> tuple[Manifest, int] | None:
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


def _copy_to_cache(src_file_path: Path,
                   dst_file_path: Path,
                   copy_filter: Callable[[BinaryIO, BinaryIO], None] | None = None) \
        -> tuple[int, str]:
    '''
    Copy a file to the cache.

    Parameters:
        src_file_path: Path to the source file.
        dst_file_path: Path to the destination file.

    Returns:
        The size of the file in bytes, after compression.
    '''
    ensure_dir_exists(dst_file_path.parent)

    if copy_filter is None:
        copy_filter = copyfileobj

    temp_dst: Path = dst_file_path.parent / f"{dst_file_path.name}.tmp"
    compressed_dst_file_name = f"{dst_file_path.name}.lz4"
    compressed_dst_file_path = dst_file_path.parent / compressed_dst_file_name
    with open(src_file_path, "rb") as file_in:
        with lz4.frame.open(temp_dst, mode="wb") as file_out:
            copy_filter(file_in, cast(BinaryIO, file_out))

    temp_dst.replace(compressed_dst_file_path)
    return compressed_dst_file_path.stat().st_size, compressed_dst_file_name


def copy_from_cache(src_file: Path,
                    dst_file: Path,
                    copy_filter: Callable[[BinaryIO, BinaryIO], None] | None = None):
    '''
    Copy a file from the cache.

    Parameters:
        src_file_path: Path to the source file.
        dst_file_path: Path to the destination file.
    '''
    ensure_dir_exists(dst_file.absolute().parent)

    if copy_filter is None:
        copy_filter = copyfileobj

    temp_dst: Path = dst_file.parent / f"{dst_file.name}.tmp"

    with lz4.frame.open(f"{src_file}.lz4", mode="rb") as file_in:
        with open(temp_dst, "wb") as file_out:
            copy_filter(cast(BinaryIO, file_in), file_out)

    temp_dst.replace(dst_file)
