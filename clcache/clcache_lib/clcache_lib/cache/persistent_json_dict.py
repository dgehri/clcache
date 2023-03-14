import contextlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict

from atomicwrites import atomic_write

from .cache_lock import CacheLock


class PersistentJsonDict:
    '''A dictionary that is persisted to a JSON file'''

    def __init__(self, file_name: Path):
        super().__init__()
        self._file_name = file_name
        self._dict = defaultdict(int)
        self._mtime = None
        with CacheLock.for_path(file_name):
            if self._file_name.exists():
                self._load()

    def _load(self):
        with contextlib.suppress(Exception):
            self._mtime = self._file_name.stat().st_mtime
            with open(self._file_name, "r") as f:
                for key, value in json.load(f).items():
                    self._dict[key] = value

    def save(self, callback=None):
        with contextlib.suppress(Exception):
            with CacheLock.for_path(self._file_name):

                # if on-disk file has changed, reload it first
                if self._file_name.exists() and self._mtime != self._file_name.stat().st_mtime:
                    self._load()

                dict_to_save = self._dict
                if callback:
                    dict_to_save = callback(self._dict)

                with atomic_write(self._file_name, overwrite=True) as f:
                    json.dump(dict_to_save, f, sort_keys=True, indent=4)

                self._mtime = self._file_name.stat().st_mtime

    def save_combined(self, other: Dict[str, int]):
        # do nothing if the other dictionary is empty, or all values are zero
        if not other or all(value == 0 for value in other.values()):
            return

        def _combine(d: Dict[str, int]) -> Dict[str, int]:
            for key, value in other.items():
                d[key] += value
            return d

        self.save(_combine)

    def __getitem__(self, key):
        return self._dict[key]

    def __setitem__(self, key, value):
        self._dict[key] = value

    # def __setitem__(self, key, value):
    #     if self._dict[key] != value:
    #         self._dict[key] = value
    #         self._mtime = None

    def __contains__(self, key):
        return key in self._dict

    def __iter__(self):
        return iter(self._dict)
