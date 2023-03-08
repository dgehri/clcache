from enum import Enum
from pathlib import Path
from collections import defaultdict
from typing import Dict
from .persistent_json_dict import PersistentJsonDict


class MissReason(Enum):
    HEADER_CHANGED_MISS = "HeaderChangedMisses"  # Header file changed
    SOURCE_CHANGED_MISS = "SourceChangedMisses"  # Source file changed
    CALL_WITH_INVALID_ARGUMENT = "CallsWithInvalidArgument"
    CALL_WITHOUT_SOURCE_FILE = "CallsWithoutSourceFile"
    CALL_WITH_MULTIPLE_SOURCE_FILES = "CallsWithMultipleSourceFiles"
    CALL_WITH_PCH = "CallsWithPch"
    CALL_FOR_LINKING = "CallsForLinking"
    CALL_FOR_EXTERNAL_DEBUG_INFO = "CallsForExternalDebugInfo"
    CALL_FOR_PREPROCESSING = "CallsForPreprocessing"
    REMOTE_CACHE_HIT = "RemoteCacheHits"


class HitReason(Enum):
    CACHE_HIT = "CacheHits"


class CacheStats(Enum):
    CACHE_ENTRIES = "CacheEntries"
    CACHE_SIZE = "CacheSize"


class Stats:
    def __init__(self):
        self._stats = defaultdict(int)

    def record_cache_miss(self, reason: MissReason):
        '''Record a cache miss'''
        self._stats[reason.value] += 1

    def record_cache_hit(self):
        '''Record a cache hit'''
        self._stats[HitReason.CACHE_HIT.value] += 1

    def register_cache_entry(self, reason: MissReason):
        '''Register a new cache entry'''
        self._stats[CacheStats.CACHE_ENTRIES.value] += 1
        self._stats[reason.value] += 1

    def register_cache_entry_size(self, size: int):
        '''Register the size of a new cache entry'''
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

    def save_combined(self, other: Stats):
        self._dict.save_combined(other._stats)

    def get(self, enum_key) -> int:
        return self._dict[enum_key.value]

    def total_cache_hits(self) -> int:
        return self._dict[HitReason.CACHE_HIT.value]

    def total_cache_misses(self) -> int:
        return sum(self._dict[attribute.value] for attribute in MissReason) \
            - self._dict[MissReason.REMOTE_CACHE_HIT.value]

    def cache_size(self) -> int:
        return self.get(CacheStats.CACHE_SIZE)

    def set_cache_size_and_entries(self, size: int, entries: int):
        def callback(d: Dict[str, int]) -> Dict[str, int]:
            d[CacheStats.CACHE_SIZE.value] = size
            d[CacheStats.CACHE_ENTRIES.value] = entries
            return d

        self._dict.save(callback)

    def reset(self):
        def callback(d: Dict[str, int]) -> Dict[str, int]:
            for attribute in MissReason:
                d[attribute.value] = 0

            for attribute in HitReason:
                d[attribute.value] = 0
            return d

        self._dict.save(callback)
