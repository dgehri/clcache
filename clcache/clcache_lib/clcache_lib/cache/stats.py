from .cache_lock import CacheLock
from .util import PersistentJSONDict


class Statistics:
    CALLS_WITH_INVALID_ARGUMENT = "CallsWithInvalidArgument"
    CALLS_WITHOUT_SOURCE_FILE = "CallsWithoutSourceFile"
    CALLS_WITH_MULTIPLE_SOURCE_FILES = "CallsWithMultipleSourceFiles"
    CALLS_WITH_PCH = "CallsWithPch"
    CALLS_FOR_LINKING = "CallsForLinking"
    CALLS_FOR_EXTERNAL_DEBUG_INFO = "CallsForExternalDebugInfo"
    CALLS_FOR_PREPROCESSING = "CallsForPreprocessing"
    LOCAL_CACHE_HITS = "CacheHits"
    REMOTE_CACHE_HITS = "RemoteCacheHits"
    CACHE_MISSES = "CacheMisses"
    EVICTED_MISSES = "EvictedMisses"
    HEADER_CHANGED_MISSES = "HeaderChangedMisses"
    SOURCE_CHANGED_MISSES = "SourceChangedMisses"
    CACHE_ENTRIES = "CacheEntries"
    CACHE_SIZE = "CacheSize"

    RESETTABLE_KEYS = {
        CALLS_WITH_INVALID_ARGUMENT,
        CALLS_WITHOUT_SOURCE_FILE,
        CALLS_WITH_MULTIPLE_SOURCE_FILES,
        CALLS_WITH_PCH,
        CALLS_FOR_LINKING,
        CALLS_FOR_EXTERNAL_DEBUG_INFO,
        CALLS_FOR_PREPROCESSING,
        LOCAL_CACHE_HITS,
        REMOTE_CACHE_HITS,
        CACHE_MISSES,
        EVICTED_MISSES,
        HEADER_CHANGED_MISSES,
        SOURCE_CHANGED_MISSES,
    }
    NON_RESETTABLE_KEYS = {
        CACHE_ENTRIES,
        CACHE_SIZE,
    }

    def __init__(self, statsFile):
        self._statsFile = statsFile
        self._stats = None
        self.lock = CacheLock.forPath(self._statsFile)

    def __enter__(self):
        self._stats = PersistentJSONDict(self._statsFile)
        for k in Statistics.RESETTABLE_KEYS | Statistics.NON_RESETTABLE_KEYS:
            if k not in self._stats:
                self._stats[k] = 0
        return self

    def __exit__(self, typ, value, traceback):
        # Does not write to disc when unchanged
        self._stats.save()

    def __eq__(self, other):
        return type(self) is type(other) and self.__dict__ == other.__dict__

    def numCallsWithInvalidArgument(self):
        return self._stats[Statistics.CALLS_WITH_INVALID_ARGUMENT]

    def registerCallWithInvalidArgument(self):
        self._stats[Statistics.CALLS_WITH_INVALID_ARGUMENT] += 1

    def numCallsWithoutSourceFile(self):
        return self._stats[Statistics.CALLS_WITHOUT_SOURCE_FILE]

    def registerCallWithoutSourceFile(self):
        self._stats[Statistics.CALLS_WITHOUT_SOURCE_FILE] += 1

    def numCallsWithMultipleSourceFiles(self):
        return self._stats[Statistics.CALLS_WITH_MULTIPLE_SOURCE_FILES]

    def registerCallWithMultipleSourceFiles(self):
        self._stats[Statistics.CALLS_WITH_MULTIPLE_SOURCE_FILES] += 1

    def numCallsWithPch(self):
        return self._stats[Statistics.CALLS_WITH_PCH]

    def registerCallWithPch(self):
        self._stats[Statistics.CALLS_WITH_PCH] += 1

    def numCallsForLinking(self):
        return self._stats[Statistics.CALLS_FOR_LINKING]

    def registerCallForLinking(self):
        self._stats[Statistics.CALLS_FOR_LINKING] += 1

    def numCallsForExternalDebugInfo(self):
        return self._stats[Statistics.CALLS_FOR_EXTERNAL_DEBUG_INFO]

    def registerCallForExternalDebugInfo(self):
        self._stats[Statistics.CALLS_FOR_EXTERNAL_DEBUG_INFO] += 1

    def numEvictedMisses(self):
        return self._stats[Statistics.EVICTED_MISSES]

    def registerEvictedMiss(self):
        self.registerCacheMiss()
        self._stats[Statistics.EVICTED_MISSES] += 1

    def numHeaderChangedMisses(self):
        return self._stats[Statistics.HEADER_CHANGED_MISSES]

    def registerHeaderChangedMiss(self):
        self.registerCacheMiss()
        self._stats[Statistics.HEADER_CHANGED_MISSES] += 1

    def numSourceChangedMisses(self):
        return self._stats[Statistics.SOURCE_CHANGED_MISSES]

    def registerSourceChangedMiss(self):
        self.registerCacheMiss()
        self._stats[Statistics.SOURCE_CHANGED_MISSES] += 1

    def numCacheEntries(self):
        return self._stats[Statistics.CACHE_ENTRIES]

    def setNumCacheEntries(self, number):
        self._stats[Statistics.CACHE_ENTRIES] = number

    def registerCacheEntry(self, size):
        self._stats[Statistics.CACHE_ENTRIES] += 1
        self._stats[Statistics.CACHE_SIZE] += size

    def unregisterCacheEntry(self, size):
        self._stats[Statistics.CACHE_ENTRIES] -= 1
        self._stats[Statistics.CACHE_SIZE] -= size

    def currentCacheSize(self):
        return self._stats[Statistics.CACHE_SIZE]

    def setCacheSize(self, size):
        self._stats[Statistics.CACHE_SIZE] = size

    def numLocalCacheHits(self):
        return self._stats[Statistics.LOCAL_CACHE_HITS]

    def numRemoteCacheHits(self):
        return self._stats[Statistics.REMOTE_CACHE_HITS]

    def registerCacheHit(self, is_local=True):
        if is_local:
            self._stats[Statistics.LOCAL_CACHE_HITS] += 1
        else:
            self._stats[Statistics.REMOTE_CACHE_HITS] += 1

    def numCacheMisses(self):
        return self._stats[Statistics.CACHE_MISSES]

    def registerCacheMiss(self):
        self._stats[Statistics.CACHE_MISSES] += 1

    def numCallsForPreprocessing(self):
        return self._stats[Statistics.CALLS_FOR_PREPROCESSING]

    def registerCallForPreprocessing(self):
        self._stats[Statistics.CALLS_FOR_PREPROCESSING] += 1

    def resetCounters(self):
        for k in Statistics.RESETTABLE_KEYS:
            self._stats[k] = 0


def printStatistics(cache):
    template = """
clcache statistics:
  current cache dir            : {}
  cache size                   : {:,} bytes
  maximum cache size           : {:,} bytes
  cache entries                : {}
  cache hits (total)           : {} ({:.0f}%)
  cache hits (remote)          : {} ({:.0f}%)
  cache misses
    total                      : {} ({:.0f}%)
    evicted                    : {}
    header changed             : {}
    source changed             : {}
  passed to real compiler
    called w/ invalid argument : {}
    called for preprocessing   : {}
    called for linking         : {}
    called for external debug  : {}
    called w/o source          : {}
    called w/ multiple sources : {}
    called w/ PCH              : {}""".strip()

    with cache.statistics.lock, cache.statistics as stats, cache.configuration as cfg:
        total_cache_hits = stats.numLocalCacheHits() + stats.numRemoteCacheHits()
        total_cache_access = total_cache_hits + stats.numCacheMisses()

        print(
            template.format(
                str(cache),
                stats.currentCacheSize(),
                cfg.maximumCacheSize(),
                stats.numCacheEntries(),
                total_cache_hits,
                float(100 * total_cache_hits) / float(total_cache_access) if total_cache_access != 0 else 0,
                stats.numRemoteCacheHits(),
                float(100 * stats.numRemoteCacheHits()) / float(total_cache_access) if total_cache_access != 0 else 0,
                stats.numCacheMisses(),
                float(100 * stats.numCacheMisses()) / float(total_cache_access) if total_cache_access != 0 else 0,
                stats.numEvictedMisses(),
                stats.numHeaderChangedMisses(),
                stats.numSourceChangedMisses(),
                stats.numCallsWithInvalidArgument(),
                stats.numCallsForPreprocessing(),
                stats.numCallsForLinking(),
                stats.numCallsForExternalDebugInfo(),
                stats.numCallsWithoutSourceFile(),
                stats.numCallsWithMultipleSourceFiles(),
                stats.numCallsWithPch(),
            )
        )


def resetStatistics(cache):
    with cache.statistics.lock, cache.statistics as stats:
        stats.resetCounters()
