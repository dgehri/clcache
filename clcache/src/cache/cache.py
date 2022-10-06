import contextlib
import os

from .file_cache import CacheFileStrategy
from .remote_cache import CacheFileWithCouchbaseFallbackStrategy
from .ex import CacheLockException


class Cache:
    def __init__(self, cacheDirectory=None):
        if os.environ.get("CLCACHE_COUCHBASE"):
            self.strategy = CacheFileWithCouchbaseFallbackStrategy(
                os.environ.get("CLCACHE_COUCHBASE"), cacheDirectory=cacheDirectory
            )
        else:
            self.strategy = CacheFileStrategy(cacheDirectory=cacheDirectory)

    def __str__(self):
        return str(self.strategy)

    @property
    def lock(self):
        return self.strategy.lock

    @contextlib.contextmanager
    def manifestLockFor(self, key):
        with self.strategy.manifestLockFor(key):
            yield

    @property
    def configuration(self):
        return self.strategy.configuration

    @property
    def statistics(self):
        return self.strategy.statistics

    def clean(self, stats, maximumSize):
        return self.strategy.clean(stats, maximumSize)

    @contextlib.contextmanager
    def lockFor(self, key):
        with self.strategy.lockFor(key):
            yield

    def getEntry(self, key):
        return self.strategy.getEntry(key)

    def setEntry(self, key, value):
        return self.strategy.setEntry(key, value)

    def hasEntry(self, cachekey):
        return self.strategy.hasEntry(cachekey)

    def setManifest(self, manifestHash, manifest):
        self.strategy.setManifest(manifestHash, manifest)

    def getManifest(self, manifestHash):
        return self.strategy.getManifest(manifestHash)


def cleanCache(cache_ref):
    with cache_ref.lock, cache_ref.statistics as stats, cache_ref.configuration as cfg:
        cache_ref.clean(stats, cfg.maximumCacheSize())


def clearCache(cache_ref):
    with cache_ref.lock, cache_ref.statistics as stats:
        cache_ref.clean(stats, 0)


def updateCacheStatistics(cache_ref, method):
    with contextlib.suppress(CacheLockException):
        with cache_ref.statistics.lock, cache_ref.statistics as stats:
            method(stats)


def addObjectToCache(stats, cache_ref, cachekey, artifacts):
    # This function asserts that the caller locked 'section' and 'stats'
    # already and also saves them
    size = cache_ref.setEntry(cachekey, artifacts)
    if size is None:
        size = os.path.getsize(artifacts.objectFilePath)
    stats.registerCacheEntry(size)

    with cache_ref.configuration as cfg:
        return stats.currentCacheSize() >= cfg.maximumCacheSize()
