import json
import time
from atomicwrites import atomic_write

from ..utils import error

class PersistentJSONDict:
    def __init__(self, fileName):
        self._dirty = False
        self._dict = {}
        self._fileName = fileName
        try:
            with open(self._fileName, "r") as f:
                self._dict = json.load(f)
        except IOError:
            pass
        except ValueError:
            error(f"clcache: persistent json file {fileName} was broken")

    def save(self):
        if not self._dirty:
            return
        success = False
        for _ in range(60):
            try:
                with atomic_write(self._fileName, overwrite=True) as f:
                    json.dump(self._dict, f, sort_keys=True, indent=4)
                    success = True
                    break
            except Exception:
                time.sleep(1)

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
