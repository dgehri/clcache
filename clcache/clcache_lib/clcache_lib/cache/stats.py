from enum import Enum
from pathlib import Path
from collections import defaultdict
from .persistent_json_dict import PersistentJsonDict


class MissReason(Enum):
    HEADER_CHANGED_MISS = "HeaderChangedMisses"
    SOURCE_CHANGED_MISS = "SourceChangedMisses"
    CALL_WITH_INVALID_ARGUMENT = "CallsWithInvalidArgument"
    CALL_WITHOUT_SOURCE_FILE = "CallsWithoutSourceFile"
    CALL_WITH_MULTIPLE_SOURCE_FILES = "CallsWithMultipleSourceFiles"
    CALL_WITH_PCH = "CallsWithPch"
    CALL_FOR_LINKING = "CallsForLinking"
    CALL_FOR_EXTERNAL_DEBUG_INFO = "CallsForExternalDebugInfo"
    CALL_FOR_PREPROCESSING = "CallsForPreprocessing"


class HitReason(Enum):
    LOCAL_CACHE_HIT = "CacheHits"
    REMOTE_CACHE_HIT = "RemoteCacheHits"


class CacheStats(Enum):
    CACHE_ENTRIES = "CacheEntries"
    CACHE_SIZE = "CacheSize"


class Stats:
    def __init__(self):
        self._stats = defaultdict(int)

    def record_cache_miss(self, reason: MissReason):
        '''Record a cache miss'''
        self._stats[reason.value] += 1

    def record_cache_hit(self, reason: HitReason):
        '''Record a cache hit'''
        self._stats[reason.value] += 1

    def register_cache_entry(self, size):
        '''Register a new cache entry'''
        self._stats[CacheStats.CACHE_ENTRIES.value] += 1
        self._stats[CacheStats.CACHE_SIZE.value] += size

    def unregister_cache_entry(self, size):
        self._stats[CacheStats.CACHE_ENTRIES.value] -= 1
        self._stats[CacheStats.CACHE_SIZE.value] -= size

    def cache_size(self):
        return self._stats[CacheStats.CACHE_SIZE.value]

    def clear_cache_size(self):
        self._stats[CacheStats.CACHE_SIZE.value] = 0

    def clear_cache_entries(self):
        self._stats[CacheStats.CACHE_ENTRIES.value] = 0


class PersistentStats:
    '''Class used to store statistics in a persistent manner.'''

    def __init__(self, file_name: Path):
        self._file_name = file_name
        self._dict = PersistentJsonDict(self._file_name)

    def save(self):
        self._dict.save()

    def get(self, enum_key) -> int:
        return self._dict[enum_key.value]

    def total_cache_hits(self) -> int:
        return self._dict[HitReason.LOCAL_CACHE_HIT.value] + self._dict[HitReason.REMOTE_CACHE_HIT.value]

    def total_cache_misses(self) -> int:
        return sum(self._dict[attribute.value] for attribute in MissReason)

    def cache_size(self) -> int:
        return self.get(CacheStats.CACHE_SIZE)

    def set_cache_size(self, size: int):
        self._dict[CacheStats.CACHE_SIZE.value] = size

    def set_cache_entries(self, entries: int):
        self._dict[CacheStats.CACHE_ENTRIES.value] = entries

    def combine_with(self, stats: Stats):
        # Merge stats into our own
        for key, value in stats._stats.items():
            self._dict[key] += value

    def reset(self):
        for attribute in MissReason:
            self._dict[attribute.value] = 0

        for attribute in HitReason:
            self._dict[attribute.value] = 0
