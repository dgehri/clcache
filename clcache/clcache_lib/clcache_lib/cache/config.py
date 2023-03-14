from pathlib import Path

from .persistent_json_dict import PersistentJsonDict


class Configuration:
    _defaults = {"MaximumCacheSize": 40737418240}  # 40 GiB

    def __init__(self, file_name: Path):
        self._dict = PersistentJsonDict(file_name)

        for setting, default_value in self._defaults.items():
            if setting not in self._dict:
                self._dict[setting] = default_value

    def save(self):
        self._dict.save()

    def max_cache_size(self):
        assert self._dict is not None
        return self._dict["MaximumCacheSize"]

    def set_max_cache_size(self, size):
        assert self._dict is not None
        self._dict["MaximumCacheSize"] = size
