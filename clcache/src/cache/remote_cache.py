import contextlib
import hashlib
import re
import lz4.frame

from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.options import (
    ClusterOptions,
    GetAndTouchOptions,
    UpsertOptions,
    ClusterTimeoutOptions,
)

from .couchbase_ex import RawBinaryTranscoderEx

from .file_cache import *
from ..config import (
    COUCHBASE_EXPIRATION,
    COUCHBASE_CONNECT_TIMEOUT,
    COUCHBASE_GET_TIMEOUT,
)
from ..utils import trace

HashAlgorithm = hashlib.md5


class CacheCouchbaseStrategy:
    def __init__(self, url):
        self.is_bad = False
        self.local_cache = {}
        self.url = url
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
    def cluster(self):
        try:
            if not self._cluster and not self.is_bad:
                self._cluster = Cluster(f"couchbase://{self.host}", self.opts)
            return self._cluster
        except Exception as e:
            trace(f"Unable to connect to remote cache at {self.host}: {e}")
            self.is_bad = True
            return None

    @property
    def bucket(self):
        try:
            if not self._bucket:
                self._bucket = self.cluster.bucket("clcache")
            return self._bucket
        except Exception as e:
            self.is_bad = True
            return None

    @property
    def coll_manifests(self):
        try:
            if not self._coll_manifests:
                self._coll_manifests = self.bucket.collection("manifests")
            return self._coll_manifests
        except Exception as e:
            self.is_bad = True
            return None

    @property
    def coll_objects(self):
        try:
            if not self._coll_objects:
                self._coll_objects = self.bucket.collection("objects")
            return self._coll_objects
        except Exception as e:
            self.is_bad = True
            return None

    @property
    def coll_object_data(self):
        try:
            if not self._coll_object_data:
                self._coll_object_data = self.bucket.collection("objects_data")
            return self._coll_object_data
        except Exception as e:
            self.is_bad = True
            return None

    @staticmethod
    def splitHost(host):
        m = re.match(r"^(?:couchbase://)?([^:]+):([^@]+)@(\S+)$", host)
        if m is None:
            raise ValueError

        return (m[1], m[2], m[3])

    def __str__(self):
        return f"Remote Couchbase {self.host}"

    def _fetchEntry(self, key):
        if self.is_bad:
            return None
        try:
            return self._fetchEntryImpl(key)
        except Exception as e:
            self.local_cache[key] = None
            return None

    def _fetchEntryImpl(self, key):
        hasher = HashAlgorithm()
        res = self.coll_objects.get_and_touch(key, COUCHBASE_EXPIRATION)
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
            res = self.coll_object_data.get_and_touch(
                f"{key}-{i}",
                COUCHBASE_EXPIRATION,
                GetAndTouchOptions(
                    transcoder=RawBinaryTranscoderEx(), timeout=COUCHBASE_GET_TIMEOUT
                ),
            )
            obj_data.append(res.value)
            hasher.update(obj_data[-1])

        if payload["md5"] != hasher.hexdigest():
            self.coll_objects.remove(key)
            for i in range(1, chunk_count + 1):
                self.coll_object_data.remove(f"{key}-{i}")

            return None

        payload["obj"] = b"".join(obj_data)
        self.local_cache[key] = payload
        return True

    def hasEntry(self, key):
        local_cache = key in self.local_cache and self.local_cache[key] is not None
        return local_cache or self._fetchEntry(key) is not None

    def getEntryAsPayload(self, key):
        if key not in self.local_cache:
            self._fetchEntry(key)
        return None if self.local_cache[key] is None else self.local_cache[key]

    def setEntry(self, key, artifacts):
        if self.is_bad:
            return None

        assert artifacts.objectFilePath
        try:
            hasher = HashAlgorithm()

            with open(artifacts.objectFilePath, "rb") as obj_file:
                obj_data = lz4.frame.compress(obj_file.read())
                obj_view = memoryview(obj_data)
                hasher.update(obj_view)

                CHUNK_LEN = 20 * 1024 * 1024
                i = 0
                total_len = len(obj_data)
                while i * CHUNK_LEN < total_len:
                    s = i * CHUNK_LEN
                    e = s + CHUNK_LEN
                    i += 1
                    sub_key = f"{key}-{i}"
                    res = self.coll_object_data.upsert(
                        sub_key,
                        obj_view[s:e],
                        UpsertOptions(transcoder=RawBinaryTranscoderEx()),
                    )
                    if not res.success:
                        return None
                    self.coll_object_data.touch(sub_key, COUCHBASE_EXPIRATION)

                payload = {
                    "stdout": artifacts.stdout,
                    "stderr": artifacts.stderr,
                    "chunk_count": i,
                    "md5": hasher.hexdigest(),
                }
                self.coll_objects.upsert(key, payload)
                self.coll_objects.touch(key, COUCHBASE_EXPIRATION)
        except Exception:
            trace(f"Could not set {key} in Couchbase {self.url}")
            return None

    def setManifest(self, key, manifest):
        if self.is_bad:
            return None

        try:
            entries = [e._asdict() for e in manifest.entries()]
            json_object = {"entries": entries}
            self.coll_manifests.upsert(key, json_object)
            self.coll_manifests.touch(key, COUCHBASE_EXPIRATION)
        except Exception:
            trace(f"Could not set {key} in Couchbase {self.url}")

    def getManifest(self, key):
        if self.is_bad:
            return None

        try:
            res = self.coll_manifests.get_and_touch(key, COUCHBASE_EXPIRATION)
            return Manifest(
                [
                    ManifestEntry(
                        e["includeFiles"], e["includesContentHash"], e["objectHash"]
                    )
                    for e in res.content_as[dict]["entries"]
                ]
            )
        except Exception:
            self.local_cache[key] = None
            return None


class CacheFileWithCouchbaseFallbackStrategy:
    def __init__(self, url, cacheDirectory=None):
        self.local_cache = CacheFileStrategy(cacheDirectory=cacheDirectory)
        self.remote_cache = CacheCouchbaseStrategy(url)

    def __str__(self):
        return f"CacheFileWithCouchbaseFallbackStrategy local({self.local_cache}) and remote({self.remote_cache})"

    def hasEntry(self, key):
        return self.local_cache.hasEntry(key) or self.remote_cache.hasEntry(key)

    def getEntry(self, key):
        if self.local_cache.hasEntry(key):
            trace(f"Getting object {key} from local cache")
            return self.local_cache.getEntry(key)

        if payload := self.remote_cache.getEntryAsPayload(key):
            trace(f"{self} remote cache hit for {key} dumping into local cache")
            self.local_cache.setEntryFromPayload(key, payload)
            return self.local_cache.getEntry(key)

        return None

    def setEntry(self, key, artifacts):
        self.local_cache.setEntry(key, artifacts)
        self.remote_cache.setEntry(key, artifacts)

    def setManifest(self, manifestHash, manifest):
        with self.local_cache.manifestLockFor(manifestHash):
            self.local_cache.setManifest(manifestHash, manifest)
        self.remote_cache.setManifest(manifestHash, manifest)

    def getManifest(self, manifestHash):
        if local := self.local_cache.getManifest(manifestHash):
            trace(f"{self} local manifest hit for {manifestHash}")
            return local

        if remote := self.remote_cache.getManifest(manifestHash):
            with self.local_cache.manifestLockFor(manifestHash):
                self.local_cache.setManifest(manifestHash, remote)
            trace(
                f"{self} remote manifest hit for {manifestHash} writing into local cache"
            )
            return remote

        return None

    @property
    def statistics(self):
        return self.local_cache.statistics

    @property
    def configuration(self):
        return self.local_cache.configuration

    def lockFor(self, key):
        return self.local_cache.lockFor(key)

    def manifestLockFor(self, key):
        return self.local_cache.manifestLockFor(key)

    @property  # type: ignore
    @contextlib.contextmanager
    def lock(self):
        with self.local_cache.lock:
            yield

    def clean(self, stats, maximumSize):
        self.local_cache.clean(stats, maximumSize)
