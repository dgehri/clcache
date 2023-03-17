import contextlib
import hashlib
import re
from typing import Dict

import lz4.frame
from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Bucket, Cluster
from couchbase.collection import Collection
from couchbase.options import (ClusterOptions, ClusterTimeoutOptions,
                               GetAndTouchOptions, UpsertOptions)

from ..cache.stats import MissReason
from ..config import (COUCHBASE_CONNECT_TIMEOUT, COUCHBASE_EXPIRATION,
                      COUCHBASE_GET_TIMEOUT)
from ..utils.util import trace
from .couchbase_ex import RawBinaryTranscoderEx
from .file_cache import *

HashAlgorithm = hashlib.md5


class CacheCouchbaseStrategy:
    def __init__(self, url: str):
        self.is_bad = False
        self.cache: Dict[str, Optional[Dict]] = {}
        self.url: str = url
        self._cluster = None
        self._bucket = None
        self._coll_manifests = None
        self._coll_objects = None
        self._coll_object_data = None
        (self.user, self.pwd, self.host) = CacheCouchbaseStrategy.splitHost(url)

        self.opts = ClusterOptions(
            authenticator=PasswordAuthenticator(self.user, self.pwd),
            timeout_options=ClusterTimeoutOptions(
                resolve_timeout=COUCHBASE_CONNECT_TIMEOUT,
                connect_timeout=COUCHBASE_CONNECT_TIMEOUT,
                bootstrap_timeout=COUCHBASE_CONNECT_TIMEOUT,
            ),
        )

    @property
    def cluster(self) -> Cluster:  # sourcery skip: raise-specific-error
        if self.is_bad:
            raise Exception("Bad cluster")

        if not self._cluster:
            self._cluster = Cluster(
                f"couchbase://{self.host}", self.opts)  # type: ignore
        return self._cluster

    @property
    def bucket(self) -> Bucket:  # sourcery skip: raise-specific-error
        if self.is_bad:
            raise Exception("Bad bucket")

        if not self._bucket:
            self._bucket = self.cluster.bucket("clcache")
        return self._bucket

    @property
    def coll_manifests(self) -> Optional[Collection]:
        try:
            if not self._coll_manifests:
                self._coll_manifests = self.bucket.collection("manifests")
            return self._coll_manifests
        except Exception as e:
            self.is_bad = True
            return None

    @property
    def coll_objects(self) -> Optional[Collection]:
        try:
            if not self._coll_objects:
                self._coll_objects = self.bucket.collection("objects")
            return self._coll_objects
        except Exception as e:
            self.is_bad = True
            return None

    @property
    def coll_object_data(self) -> Optional[Collection]:
        try:
            if not self._coll_object_data:
                self._coll_object_data = self.bucket.collection("objects_data")
            return self._coll_object_data
        except Exception as e:
            self.is_bad = True
            return None

    @staticmethod
    def splitHost(host: str) -> Tuple[str, str, str]:
        m = re.match(r"^(?:couchbase://)?([^:]+):([^@]+)@(\S+)$", host)
        if m is None:
            raise ValueError

        return (m[1], m[2], m[3])

    def __str__(self):
        return f"Remote Couchbase {self.host}"

    def _fetchEntry(self, key: str) -> Optional[bool]:
        if self.is_bad:
            return None
        try:
            return self._fetchEntryImpl(key)
        except Exception:
            self.cache[key] = None
            return None

    def _fetchEntryImpl(self, key: str) -> Optional[bool]:
        '''Fetches an entry from the cache and stores it in self.cache.'''
        hasher = HashAlgorithm()
        coll_o = self.coll_objects
        if not coll_o:
            return None

        coll_data = self.coll_object_data
        if not coll_data:
            return None

        res = coll_o.get_and_touch(key, COUCHBASE_EXPIRATION)
        payload = res.content_as[dict]
        if "chunk_count" not in payload:
            return None
        if "md5" not in payload:
            return None
        if "stdout" not in payload:
            return None
        if "stderr" not in payload:
            return None

        chunk_count = payload["chunk_count"]
        obj_data = []
        for i in range(1, chunk_count + 1):
            res = coll_data.get_and_touch(
                f"{key}-{i}",
                COUCHBASE_EXPIRATION,
                GetAndTouchOptions(
                    transcoder=RawBinaryTranscoderEx(), timeout=COUCHBASE_GET_TIMEOUT
                ),  # type: ignore
            )
            obj_data.append(res.value)
            hasher.update(obj_data[-1])

        if payload["md5"] != hasher.hexdigest():
            coll_o.remove(key)
            for i in range(1, chunk_count + 1):
                coll_data.remove(f"{key}-{i}")

            return None

        payload["obj"] = b"".join(obj_data)
        self.cache[key] = payload
        return True

    def has_entry(self, key: str):
        '''
        Returns true if the cache contains an entry for the given key.

        Returns:
            A tuple of (has entry, is local cache entry).
        '''
        in_cache = key in self.cache and self.cache[key] is not None
        return in_cache or self._fetchEntry(key) is not None, False

    def getEntryAsPayload(self, key: str) -> Optional[dict]:
        '''
        Returns the entry as a dict, or None if it is not in the cache.
        '''
        if key not in self.cache:
            self._fetchEntry(key)
        return None if self.cache[key] is None else self.cache[key]

    def set_entry(self, key: str, artifacts: CompilerArtifacts) -> int:
        '''
        Stores the given artifacts in the cache.

        Returns:
            The number of bytes stored in the cache. 0 if the entry was not stored.
        '''
        if self.is_bad:
            return 0

        assert artifacts.obj_file_path
        try:
            with open(artifacts.obj_file_path, "rb") as obj_file:
                return self._setEntryFromFile(obj_file, key, artifacts)
        except Exception:
            trace(f"Could not set {key} in Couchbase {self.url}")
            return 0

    def _setEntryFromFile(self, obj_file, key: str, artifacts: CompilerArtifacts) -> int:
        '''
        Stores the given artifacts in the cache.

        Returns:
            The number of bytes stored in the cache. 0 if the entry was not stored.
        '''
        coll_o = self.coll_objects
        if not coll_o:
            return 0

        coll_data = self.coll_object_data
        if not coll_data:
            return 0

        obj_data = lz4.frame.compress(obj_file.read())
        obj_view = memoryview(obj_data)
        hasher = HashAlgorithm()
        hasher.update(obj_view)

        CHUNK_LEN = 20 * 1024 * 1024
        i = 0
        total_len = len(obj_data)
        while i * CHUNK_LEN < total_len:
            s = i * CHUNK_LEN
            e = s + CHUNK_LEN
            i += 1
            sub_key = f"{key}-{i}"
            res = coll_data.upsert(
                sub_key,
                obj_view[s:e],  # type: ignore
                UpsertOptions(\
                    transcoder=RawBinaryTranscoderEx())  # type: ignore
                ,
            )
            if not res.success:
                return 0
            coll_data.touch(sub_key, COUCHBASE_EXPIRATION)

        payload = {
            "stdout": artifacts.stdout,
            "stderr": artifacts.stderr,
            "chunk_count": i,
            "md5": hasher.hexdigest(),
        }
        coll_o.upsert(key, payload)
        coll_o.touch(key, COUCHBASE_EXPIRATION)
        return len(obj_view)

    def set_manifest(self, key: str, manifest: Manifest):
        if not self.is_bad:
            try:
                entries = [e._asdict() for e in manifest.entries()]
                json_object = {"entries": entries}
                if coll_manifests := self.coll_manifests:
                    coll_manifests.upsert(key, json_object)
                    coll_manifests.touch(key, COUCHBASE_EXPIRATION)
            except Exception:
                trace(f"Could not set {key} in Couchbase {self.url}")

    def get_manifest(self, key: str) -> Optional[Manifest]:
        if self.is_bad:
            return None

        try:
            coll_manifests = self.coll_manifests
            if not coll_manifests:
                return None
            res = coll_manifests.get_and_touch(key, COUCHBASE_EXPIRATION)
            return Manifest(
                [
                    ManifestEntry(
                        e["includeFiles"], e["includesContentHash"], e["objectHash"]
                    )
                    for e in res.content_as[dict]["entries"]
                ]
            )
        except Exception:
            self.cache[key] = None
            return None


class CacheFileWithCouchbaseFallbackStrategy:
    def __init__(self, url, cache_dir=None):
        self.local_cache = CacheFileStrategy(cache_dir=cache_dir)
        self.remote_cache = CacheCouchbaseStrategy(url)

    def __enter__(self):
        self.local_cache.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.local_cache.__exit__(exc_type, exc_value, traceback)

    def __str__(self):
        return f"CacheFileWithCouchbaseFallbackStrategy {self.local_cache} and {self.remote_cache}"

    def has_entry(self, key: str) -> Tuple[bool, bool]:
        '''
        Returns true if the cache contains an entry for the given key.

        Returns:
            A tuple of (has entry, is local cache entry).
        '''
        local_hit, _ = self.local_cache.has_entry(key)
        return (True, True) if local_hit else self.remote_cache.has_entry(key)

    def get_entry(self, key: str) -> Optional[CompilerArtifacts]:
        '''
        Returns the cache entry, or None if it is not in the cache.

        If the entry is in the remote cache, it will be copied into the local cache.
        '''
        local_hit, _ = self.local_cache.has_entry(key)
        if local_hit:
            trace(f"Getting object {key} from local cache")
            return self.local_cache.get_entry(key)

        if payload := self.remote_cache.getEntryAsPayload(key):
            trace(f"{self} remote cache hit for {key} dumping into local cache")
            size = self.local_cache.set_entry_from_payload(key, payload)

            # record the hit, and size of the object in the stats
            self.local_cache.current_stats.register_cache_entry_size(size)
            self.local_cache.current_stats.register_cache_entry(MissReason.REMOTE_CACHE_HIT)

            return self.local_cache.get_entry(key)

        return None

    def set_entry(self, key: str, artifacts) -> int:
        '''
        Sets the cache entry.

        Returns:
            The size of the entry in bytes.
        '''
        size = self.local_cache.set_entry(key, artifacts)
        self.remote_cache.set_entry(key, artifacts)
        return size

    def set_manifest(self, manifest_hash: str, manifest: Manifest) -> int:
        '''
        Sets the manifest in the cache.

        This will also set the manifest in the remote cache.
        '''
        with self.local_cache.manifest_lock_for(manifest_hash):
            size = self.local_cache.set_manifest(manifest_hash, manifest)
        self.remote_cache.set_manifest(manifest_hash, manifest)

        return size

    def get_manifest(self, manifest_hash: str) -> Optional[Tuple[Manifest, int]]:
        '''
        Returns the manifest, or None if it is not in the cache.

        If the manifest is in the remote cache, it will be copied into the local cache.
        '''
        if local := self.local_cache.get_manifest(manifest_hash):
            trace(f"{self} local manifest hit for {manifest_hash}")
            return local

        if remote := self.remote_cache.get_manifest(manifest_hash):
            with self.local_cache.manifest_lock_for(manifest_hash):
                size = self.local_cache.set_manifest(manifest_hash, remote)

                # record the size of the manifest in the stats
                self.local_cache.current_stats.register_cache_entry_size(size)

            trace(
                f"{self} remote manifest hit for {manifest_hash} writing into local cache"
            )
            return remote, size

        return None

    @property
    def current_stats(self):
        return self.local_cache.current_stats

    @property
    def persistent_stats(self):
        return self.local_cache.persistent_stats

    @property
    def configuration(self):
        return self.local_cache.configuration

    def lock_for(self, key: str):
        return self.local_cache.lock_for(key)

    def manifest_lock_for(self, key: str):
        return self.local_cache.manifest_lock_for(key)

    @property  # type: ignore
    @contextlib.contextmanager
    def lock(self):
        with self.local_cache.lock:
            yield

    def clean(self):
        self.local_cache.clean()

    def clear(self):
        self.local_cache.clear()
