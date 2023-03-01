import contextlib
from pathlib import Path
from .cache_lock import CacheLock
from ..utils import error
import json
from atomicwrites import atomic_write
from collections import defaultdict

class PersistentJsonDict:
    '''A dictionary that is persisted to a JSON file'''

    def __init__(self, file_name: Path):
        super().__init__()
        self._file_name = file_name
        self._dict = defaultdict(int)
        self._mtime = None
        try:
            with CacheLock.for_path(file_name):
                if file_name.exists():
                    self._mtime = file_name.stat().st_mtime
                    with open(file_name, "r") as f:
                        for key, value in json.load(f).items():
                            self._dict[key] = value
        except IOError:
            pass
        except ValueError:
            error(f"clcache: persistent JSON file {file_name} broken")
            
    def __enter__(self):
        return self
    
    def __exit__(self, typ, value, traceback):
        self.save()

    def save(self):
        with contextlib.suppress(Exception):
            with CacheLock.for_path(self._file_name):
                # Only save if the file has been modified since we last read it, or if it doesn't exist
                if not self._file_name.exists() or self._mtime != self._file_name.stat().st_mtime:
                    with atomic_write(self._file_name, overwrite=True) as f:
                        json.dump(self._dict, f, sort_keys=True, indent=4)

                    self._mtime = self._file_name.stat().st_mtime

    def __getitem__(self, key):
        return self._dict[key]

    def __setitem__(self, key, value):
        if self._dict[key] != value:
            self._dict[key] = value
            self._mtime = None

    def __contains__(self, key):
        return key in self._dict

    def __iter__(self):
        return iter(self._dict)
