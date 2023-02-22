import json
import os
import time
from atomicwrites import atomic_write
from .cache_lock import CacheLock
from ..utils import error

class PersistentJSONDict:
    def __init__(self, fileName):
        self._dirty = False
        self._dict = {}
        self._base_dict = {}
        self._fileName = fileName
        try:
            with CacheLock.forPath(self._fileName):
                with open(self._fileName, "r") as f:
                    self._dict = json.load(f)
                    self._base_dict = self._dict.copy()
                self._mtime = os.path.getmtime(self._fileName)
                
        except IOError:
            pass
        except ValueError:
            error(f"clcache: persistent json file {fileName} was broken")
                            
    def dirty(self):
        return self._dirty

    def save(self):
        if not self._dirty:
            return
        
        with CacheLock.forPath(self._fileName):
            # check if file was changed by another process
            if self._mtime != os.path.getmtime(self._fileName):
                 
                # load file and merge with current dict
                with open(self._fileName, "r") as f:
                    target_dict = json.load(f)
                    
                    for key in self._dict:
                        new_value = self._dict[key] - self._base_dict[key] + target_dict[key]
                        self._dict[key] = new_value

            success = False
            for _ in range(60):
                try:
                    with atomic_write(self._fileName, overwrite=True) as f:
                        json.dump(self._dict, f, sort_keys=True, indent=4)
                        success = True
                        break
                except Exception:
                    time.sleep(.1)

            if not success:
                with atomic_write(self._fileName, overwrite=True) as f:
                    json.dump(self._dict, f, sort_keys=True, indent=4)

    def __setitem__(self, key, value):
        self._dict[key] = value
        self._dirty = True

    def __getitem__(self, key):
        return self._dict[key]

    def __contains__(self, key):
        return key in self._dict

    def __eq__(self, other):
        return type(self) is type(other) and self.__dict__ == other.__dict__
