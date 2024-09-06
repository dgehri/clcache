import contextlib
import os
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import BinaryIO

from ..config.config import VERSION
from ..utils.logging import LogLevel, log
from .file_cache import CacheFileStrategy, CompilerArtifacts
from .stats import CacheStats, MissReason, PersistentStats, Stats


class Location(IntEnum):
    LOCAL = 1,
    REMOTE = 2,
    LOCAL_AND_REMOTE = 3
    
class Cache:
    def __init__(self, cache_dir: Path | None = None):
        if url := os.environ.get("CLCACHE_COUCHBASE"):
            try:
                from .remote_cache import \
                    CacheFileWithCouchbaseFallbackStrategy
                self.strategy = CacheFileWithCouchbaseFallbackStrategy(
                    url, cache_dir=cache_dir)
                return
            except Exception as e:
                log(
                    f"Failed to initialize Couchbase cache using {url}", LogLevel.WARN)

        self.strategy = CacheFileStrategy(cache_dir=cache_dir)

    def __enter__(self):
        self.strategy.__enter__()
        return self

    def __exit__(self, typ, value, traceback):
        self.strategy.__exit__(typ, value, traceback)

    def __str__(self):
        return str(self.strategy)

    @property
    def lock(self):
        return self.strategy.lock

    @contextlib.contextmanager
    def manifest_lock_for(self, key: str):
        with self.strategy.manifest_lock_for(key):
            yield

    @property
    def configuration(self):
        return self.strategy.configuration

    @property
    def statistics(self) -> Stats:
        return self.strategy.current_stats

    @property
    def persistent_stats(self) -> PersistentStats:
        return self.strategy.persistent_stats

    def clean(self):
        self.strategy.clean()

    def clear(self):
        self.strategy.clear()

    @contextlib.contextmanager
    def lock_for(self, key: str):
        with self.strategy.lock_for(key):
            yield

    def get_entry(self, key: str):
        return self.strategy.get_entry(key)

    def set_entry(self, key: str, value):
        return self.strategy.set_entry(key, value)

    def has_entry(self, cachekey) -> bool:
        '''
        Returns true if the cache contains an entry for the given key.

        Returns:
            A tuple of (has entry, is local cache entry).
        '''
        return self.strategy.has_entry(cachekey)

    def set_manifest(self, manifest_hash, manifest, location = Location.LOCAL_AND_REMOTE):
        return self.strategy.set_manifest(manifest_hash, manifest, location)

    def get_manifest(self, manifest_hash: str, skip_remote: bool=False):
        return self.strategy.get_manifest(manifest_hash, skip_remote)


def clean_cache(cache: Cache):
    with cache.lock:
        cache.clean()


def clear_cache(cache: Cache):
    with cache.lock:
        cache.clear()


def ensure_artifacts_exist(cache: Cache,
                           cache_key: str,
                           reason: MissReason,
                           payload: Path,
                           compiler_result: tuple[int, str, str],
                           canonicalize_stdout: None | (Callable[[
                               str], str]) = None,
                           canonicalize_stderr: None | (Callable[[
                               str], str]) = None,
                           post_commit_action: None | (Callable[[
                           ], int]) = None,
                           output_file_filter: Callable[[BinaryIO, BinaryIO], None] | None = None
                           ) -> tuple[int, str, str]:
    '''
    Ensure that the artifacts for the given cache key exist.

    Parameters:
        cache: The cache to use.
        cache_key: The cache key to use.
        reason: The reason for the cache miss.
        payload: The path to the payload file.
        compiler_result: The result of the compiler invocation.
        canonicalize_stdout: A function to canonicalize the compiler stdout.
        canonicalize_stderr: A function to canonicalize the compiler stderr.
        post_commit_action: An action to execute after the cache entry was added.
        output_file_filter: A function to filter the output files.

    Returns:
        tuple[int, str, str]: A tuple containing the exit code, stdout and stderr.
    '''
    return_code, compiler_stdout, compiler_stderr = compiler_result
    if return_code == 0 and payload.exists():
        artifacts = CompilerArtifacts(
            payload,
            canonicalize_stdout(
                compiler_stdout) if canonicalize_stdout else compiler_stdout,
            canonicalize_stderr(
                compiler_stderr) if canonicalize_stderr else compiler_stderr,
            output_file_filter
        )

        log(
            f"Adding file {artifacts.payload_path} to cache using key {cache_key}")

        _add_object_to_cache(cache, cache_key, artifacts,
                             reason, post_commit_action)

    return return_code, compiler_stdout, compiler_stderr


def _add_object_to_cache(cache: Cache,
                         cache_key: str,
                         artifacts: CompilerArtifacts,
                         reason: MissReason,
                         post_commit_action: Callable[[], int] | None = None):

    size: int = 0

    with cache.lock_for(cache_key):
        # If the cache entry is not present, add it.
        if not cache.has_entry(cache_key):
            cache.statistics.register_cache_entry(reason)

            size = cache.set_entry(cache_key, artifacts)
            if size is None:
                size = os.path.getsize(artifacts.payload_path)

    if post_commit_action:
        # Always execute the action, even if the cache entry was present.
        size += post_commit_action()

    cache.statistics.register_cache_entry_size(size)


def is_cache_cleanup_required(cache: Cache):
    return cache.persistent_stats.cache_size() + \
        cache.statistics.cache_size() > cache.configuration.max_cache_size()


def print_statistics(cache: Cache):
    template = """
clcache {} statistics:
  current cache dir            : {}
  cache size                   : {:,.1f} MB
  maximum cache size           : {:,.0f} GB
  cache entries                : {}
  cache hits (total)           : {} ({:.0f}%)
  cache hits (remote)          : {} ({:.0f}%)
  cache misses                 : {} ({:.0f}%)
    header changed             : {}
    source changed             : {}
    cache failure              : {}
    called w/ invalid argument : {}
    called for preprocessing   : {}
    called for linking         : {}
    called for external debug  : {}
    called w/o source          : {}
    called w/ multiple sources : {}
    called w/ PCH              : {}""".strip()

    stats = cache.persistent_stats
    cfg = cache.configuration
    total_cache_hits = stats.total_cache_hits()
    total_cache_misses = stats.total_cache_misses()
    total_cache_access = total_cache_hits + total_cache_misses

    print(
        template.format(
            VERSION,
            str(cache),
            stats.get(CacheStats.CACHE_SIZE) / 1024 / 1024,
            cfg.max_cache_size() / 1024 / 1024 / 1024,
            stats.get(CacheStats.CACHE_ENTRIES),
            total_cache_hits,
            float(100 * total_cache_hits) /
            float(total_cache_access) if total_cache_access != 0 else 0,
            stats.get(MissReason.REMOTE_CACHE_HIT),
            float(100 * stats.get(MissReason.REMOTE_CACHE_HIT)) /
            float(total_cache_access) if total_cache_access != 0 else 0,
            total_cache_misses,
            float(100 * total_cache_misses) /
            float(total_cache_access) if total_cache_access != 0 else 0,
            stats.get(MissReason.HEADER_CHANGED_MISS),
            stats.get(MissReason.SOURCE_CHANGED_MISS),
            stats.get(MissReason.CACHE_FAILURE),
            stats.get(MissReason.CALL_WITH_INVALID_ARGUMENT),
            stats.get(MissReason.CALL_FOR_PREPROCESSING),
            stats.get(MissReason.CALL_FOR_LINKING),
            stats.get(MissReason.CALL_FOR_EXTERNAL_DEBUG_INFO),
            stats.get(MissReason.CALL_WITHOUT_SOURCE_FILE),
            stats.get(MissReason.CALL_WITH_MULTIPLE_SOURCE_FILES),
            stats.get(MissReason.CALL_WITH_PCH)
        )
    )


def reset_stats(cache: Cache):
    cache.persistent_stats.reset()
