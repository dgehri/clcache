import contextlib
import hashlib
import re
from typing import BinaryIO, Dict

from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Bucket, Cluster
from couchbase.collection import Collection
from couchbase.options import (ClusterOptions, ClusterTimeoutOptions,
                               GetAndTouchOptions, RemoveOptions, TouchOptions,
                               UpsertOptions)

from ..cache.cache import Location
from ..cache.stats import MissReason
from ..config import (COUCHBASE_ACCESS_TIMEOUT, COUCHBASE_CONNECT_TIMEOUT,
                      COUCHBASE_EXPIRATION)
from .couchbase_ex import RawBinaryTranscoderEx
from .file_cache import *

HashAlgorithm = hashlib.md5


# Declare excatpion to signal that remote cache is bad
class CacheBadException(Exception):
    pass


def make_bad_if_exception(func):
    def wrapper(self, *args, **kwargs):
        try:
            if not self._is_bad:
                if result := func(self, *args, **kwargs):
                    return result

            raise CacheBadException

        except CacheBadException:
            self._is_bad = True
            raise
        except Exception as e:
            self._is_bad = True
            raise CacheBadException from e

    return wrapper


def verify_success(result):
    if not result.success:
        raise CacheBadException


class CacheCouchbaseStrategy:

    def __init__(self, url: str):
        self._is_bad = False
        self._cache: Dict[str, Optional[Dict]] = {}
        self._url: str = url
        self.__cluster = None
        self.__bucket = None
        self.__coll_manifests = None
        self.__coll_objects = None
        self.__coll_object_data = None
        (self.user, self.pwd, self.host) = CacheCouchbaseStrategy._split_host(url)

        self._opts = ClusterOptions(
            authenticator=PasswordAuthenticator(self.user, self.pwd),
            timeout_options=ClusterTimeoutOptions(
                resolve_timeout=COUCHBASE_CONNECT_TIMEOUT,
                connect_timeout=COUCHBASE_CONNECT_TIMEOUT,
                bootstrap_timeout=COUCHBASE_CONNECT_TIMEOUT,
            ),
        )

    @property
    @make_bad_if_exception
    def _cluster(self) -> Cluster:
        if not self.__cluster:
            self.__cluster = Cluster(
                f"couchbase://{self.host}", self._opts)  # type: ignore
        return self.__cluster

    @property
    @make_bad_if_exception
    def _bucket(self) -> Bucket:
        if not self.__bucket:
            self.__bucket = self._cluster.bucket("clcache")
        return self.__bucket

    @property
    @make_bad_if_exception
    def _coll_manifests(self) -> Collection:
        if not self.__coll_manifests:
            self.__coll_manifests = self._bucket.collection("manifests")
        return self.__coll_manifests

    @property
    @make_bad_if_exception
    def _coll_objects(self) -> Collection:
        if not self.__coll_objects:
            self.__coll_objects = self._bucket.collection("objects")
        return self.__coll_objects

    @property
    @make_bad_if_exception
    def _coll_object_data(self) -> Collection:
        if not self.__coll_object_data:
            self.__coll_object_data = self._bucket.collection(
                "objects_data")
        return self.__coll_object_data

    @staticmethod
    def _split_host(host: str) -> Tuple[str, str, str]:
        m = re.match(r"^(?:couchbase://)?([^:]+):([^@]+)@(\S+)$", host)
        if m is None:
            raise ValueError

        return (m[1], m[2], m[3])

    def __str__(self):
        return f"Remote Couchbase {self.host}"

    def _fetch_entry(self, key: str) -> Optional[bool]:
        try:
            return self._fetch_entry_impl(key)
        except Exception:
            self._is_bad = True
            self._cache[key] = None
            return None

    def _fetch_entry_impl(self, key: str) -> Optional[bool]:
        '''Fetches an entry from the cache and stores it in self.cache.'''

        res = self._coll_objects.get_and_touch(key, COUCHBASE_EXPIRATION,
                                               GetAndTouchOptions(
                                                   timeout=COUCHBASE_ACCESS_TIMEOUT))  # type: ignore
        verify_success(res)

        payload = res.content_as[dict]

        if "chunk_count" not in payload \
                or "md5" not in payload \
                or "stdout" not in payload \
                or "stderr" not in payload:
            return None

        chunk_count = payload["chunk_count"]
        obj_data = []
        hasher = HashAlgorithm()
        for i in range(1, chunk_count + 1):
            res = self._coll_object_data.get_and_touch(
                f"{key}-{i}",
                COUCHBASE_EXPIRATION,
                GetAndTouchOptions(
                    transcoder=RawBinaryTranscoderEx(), timeout=COUCHBASE_ACCESS_TIMEOUT
                ),  # type: ignore
            )
            verify_success(res)

            obj_data.append(res.value)
            hasher.update(obj_data[-1])

        if payload["md5"] != hasher.hexdigest():
            res = self._coll_objects.remove(key)
            verify_success(res)
            for i in range(1, chunk_count + 1):
                res = self._coll_object_data.remove(f"{key}-{i}",
                                                    RemoveOptions(timeout=COUCHBASE_ACCESS_TIMEOUT))  # type: ignore
                verify_success(res)

            return None

        payload["obj"] = b"".join(obj_data)
        self._cache[key] = payload
        return True

    def has_entry(self, key: str) -> bool:
        '''
        Returns true if the cache contains an entry for the given key.

        Returns:
            A tuple of (has entry, is local cache entry).
        '''
        in_cache = key in self._cache and self._cache[key] is not None
        return in_cache or self._fetch_entry(key) is not None

    def get_entry_as_payload(self, key: str) -> Optional[dict]:
        '''
        Returns the entry as a dict, or None if it is not in the cache.
        '''
        if key not in self._cache:
            self._fetch_entry(key)
        return None if self._cache[key] is None else self._cache[key]

    def set_entry_from_compressed(self,
                                  key: str,
                                  artifacts: CompilerArtifacts,
                                  compressed_payload_path: Path):
        '''
        Stores the given artifacts in the cache.

        Returns:
            The number of bytes stored in the cache. 0 if the entry was not stored.
        '''
        if not self._is_bad:
            try:
                with open(compressed_payload_path, "rb") as obj_file:
                    self._set_entry_from_compressed_file(
                        obj_file, key, artifacts)
            except Exception:
                self._is_bad = True
                log(f"Could not set {key} in remote cache",
                    level=LogLevel.TRACE)

    def _set_entry_from_compressed_file(self,
                                        obj_file: BinaryIO,
                                        key: str,
                                        artifacts: CompilerArtifacts):
        '''
        Stores the given artifacts in the cache.

        Returns:
            The number of bytes stored in the cache. 0 if the entry was not stored.
        '''

        obj_data = obj_file.read()
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
            res = self._coll_object_data.upsert(
                sub_key,
                obj_view[s:e],  # type: ignore
                UpsertOptions(
                    transcoder=RawBinaryTranscoderEx(),
                    timeout=COUCHBASE_ACCESS_TIMEOUT)  # type: ignore
                ,
            )
            verify_success(res)

            res = self._coll_object_data.touch(sub_key, COUCHBASE_EXPIRATION, TouchOptions(
                timeout=COUCHBASE_ACCESS_TIMEOUT))  # type: ignore
            verify_success(res)

        payload = {
            "stdout": artifacts.stdout,
            "stderr": artifacts.stderr,
            "chunk_count": i,
            "md5": hasher.hexdigest(),
        }
        res = self._coll_objects.upsert(key, payload, UpsertOptions(
            timeout=COUCHBASE_ACCESS_TIMEOUT))  # type: ignore
        verify_success(res)

        res = self._coll_objects.touch(
            key, COUCHBASE_EXPIRATION, timeout=COUCHBASE_ACCESS_TIMEOUT)  # type: ignore
        verify_success(res)

    def set_manifest(self, key: str, manifest: Manifest):
        '''
        Set the manifest in the remote cache

        Important:
            Even though we attempt to merge the manifests, this is not atomic.
            Therefore, it is possible that another process will write a manifest
            between the time we fetch the existing manifest and the time we write
            the new manifest.

            Also, we are likely to write a manifest referencing object keys 
            that do not exist in the remote cache. This is not a major issue, as
            the result is simply that retrieving the object will fail.
        '''
        if self._is_bad:
            return

        try:
            # First fetch existing manifest
            if remote_manifest := self.get_manifest(key):
                # Merge the manifests
                entries = list(
                    set(remote_manifest.entries() + manifest.entries()))
                manifest = Manifest(entries)

            entries = [e._asdict() for e in manifest.entries()]
            json_object = {"entries": entries}
            if coll_manifests := self._coll_manifests:
                res = coll_manifests.upsert(key, json_object, UpsertOptions(
                    timeout=COUCHBASE_ACCESS_TIMEOUT))  # type: ignore
                verify_success(res)
                res = coll_manifests.touch(key, COUCHBASE_EXPIRATION)
                verify_success(res)
        except Exception:
            self._is_bad = True
            log(f"Could not set {key} in remote cache", level=LogLevel.TRACE)

    @functools.cache
    def get_manifest(self, key: str) -> Optional[Manifest]:
        if self._is_bad:
            return None

        try:
            res = self._coll_manifests.get_and_touch(
                key, COUCHBASE_EXPIRATION,
                GetAndTouchOptions(timeout=COUCHBASE_ACCESS_TIMEOUT))  # type: ignore
            verify_success(res)
            return Manifest(
                [
                    ManifestEntry(
                        e["includeFiles"],
                        e["includesContentHash"],
                        e["objectHash"]
                    )
                    for e in res.content_as[dict]["entries"]
                ]
            )
        except Exception:
            self._cache[key] = None
            self._is_bad = True
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

    def has_entry(self, key: str) -> bool:
        '''
        Returns true if the cache contains an entry for the given key.

        Returns:
            A tuple of (has entry, is local cache entry).
        '''
        hit = self.local_cache.has_entry(key)
        return True if hit else self.remote_cache.has_entry(key)

    def get_entry(self, key: str) -> Optional[CompilerArtifacts]:
        '''
        Returns the cache entry, or None if it is not in the cache.

        If the entry is in the remote cache, it will be copied into the local cache.
        '''
        if self.local_cache.has_entry(key):
            log(f"Fetching object {key} from local cache")
            return self.local_cache.get_entry(key)

        if payload := self.remote_cache.get_entry_as_payload(key):
            log(f"Dumping remote cache hit for {key} into local cache")
            size = self.local_cache.set_entry_from_payload(key, payload)

            # record the hit, and size of the object in the stats
            self.local_cache.current_stats.register_cache_entry_size(size)
            self.local_cache.current_stats.register_cache_entry(
                MissReason.REMOTE_CACHE_HIT)

            return self.local_cache.get_entry(key)

        return None

    def set_entry(self, key: str, artifacts) -> int:
        '''
        Sets the cache entry.

        Returns:
            The size of the entry in bytes.
        '''
        size, compressed_payload_path = self.local_cache.set_entry_ex(
            key, artifacts)
        if compressed_payload_path:
            self.remote_cache.set_entry_from_compressed(
                key, artifacts, compressed_payload_path)
        return size

    def set_manifest(self,
                     manifest_hash: str,
                     manifest: Manifest,
                     location=Location.LOCAL_AND_REMOTE) -> int:
        '''
        Sets the manifest in the cache.

        This will also set the manifest in the remote cache.
        '''
        size = 0
        if location & Location.LOCAL:
            with self.local_cache.manifest_lock_for(manifest_hash):
                size = self.local_cache.set_manifest(
                    manifest_hash, manifest, location)

        if location & Location.REMOTE:
            self.remote_cache.set_manifest(manifest_hash, manifest)

        return size

    def get_manifest(self, manifest_hash: str, skip_remote: bool) -> Optional[Tuple[Manifest, int]]:
        '''
        Returns the manifest, or None if it is not in the cache.

        If the manifest is in the remote cache, it will be copied into the local cache.
        '''
        if local := self.local_cache.get_manifest(manifest_hash):
            log(f"Local manifest hit for {manifest_hash}")
            return local

        if not skip_remote:
            if remote := self.remote_cache.get_manifest(manifest_hash):
                with self.local_cache.manifest_lock_for(manifest_hash):
                    size = self.local_cache.set_manifest(
                        manifest_hash, remote, Location.LOCAL)

                    # record the size of the manifest in the stats
                    self.local_cache.current_stats.register_cache_entry_size(
                        size)

                log(
                    f"Remote manifest hit for {manifest_hash}, writing into local cache"
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
