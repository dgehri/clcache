import contextlib
import os
from pathlib import Path
from typing import Callable, Optional, Tuple

from .stats import CacheStats, MissReason, PersistentStats, Stats
from ..utils.util import trace
from .file_cache import CacheFileStrategy, CompilerArtifacts


class Cache:
    def __init__(self, cache_dir: Optional[Path] = None):
        if url := os.environ.get("CLCACHE_COUCHBASE"):
            try:
                from .remote_cache import CacheFileWithCouchbaseFallbackStrategy
                self.strategy = CacheFileWithCouchbaseFallbackStrategy(
                    url, cacheDirectory=cache_dir)
                return
            except Exception as e:
                trace(f"Failed to initialize Couchbase cache using {url}")

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

    def has_entry(self, cachekey) -> Tuple[bool, bool]:
        '''
        Returns true if the cache contains an entry for the given key.

        Returns:
            A tuple of (has entry, is local cache entry).
        '''
        return self.strategy.has_entry(cachekey)

    def set_manifest(self, manifest_hash, manifest):
        return self.strategy.set_manifest(manifest_hash, manifest)

    def get_manifest(self, manifest_hash):
        return self.strategy.get_manifest(manifest_hash)


def clean_cache(cache: Cache):
    with cache.lock:
        cache.clean()


def clear_cache(cache: Cache):
    with cache.lock:
        cache.clear()


def add_object_to_cache(cache: Cache,
                        cache_key: str,
                        artifacts: CompilerArtifacts,
                        reason: MissReason,
                        action: Optional[Callable[[], int]] = None):

    size = 0

    with cache.lock_for(cache_key):
        hit, _ = cache.has_entry(cache_key)
        # If the cache entry is not present, add it.
        if not hit:
            cache.statistics.register_cache_entry(reason)

            size = cache.set_entry(cache_key, artifacts)
            if size is None:
                size = os.path.getsize(artifacts.obj_file_path)

    if action:
        # Always execute the action, even if the cache entry was present.
        size += action()

    cache.statistics.register_cache_entry_size(size)


def is_cache_cleanup_required(cache: Cache):
    return cache.persistent_stats.cache_size() + \
        cache.statistics.cache_size() > cache.configuration.max_cache_size()


def print_statistics(cache: Cache):
    template = """
clcache statistics:
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
