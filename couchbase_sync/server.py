import contextlib
from datetime import timedelta
import functools
import hashlib
from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Bucket, Cluster
from couchbase.collection import Collection
from couchbase.options import (
    ClusterOptions,
    ClusterTimeoutOptions,
    GetOptions,
    RemoveOptions,
    TouchOptions,
    UpsertOptions,
)
from couchbase.transcoder import *  # type: ignore
from cache import Manifest, ManifestEntry

COUCHBASE_CONNECT_TIMEOUT = timedelta(seconds=10)
COUCHBASE_ACCESS_TIMEOUT = timedelta(seconds=10)
COUCHBASE_EXPIRATION = timedelta(days=3)
HashAlgorithm = hashlib.md5


class CacheBadException(Exception):
    pass


def verify_success(result):
    if not result.success:
        raise CacheBadException


class CouchbaseServer:
    def __init__(self, server, username, password):
        self.host = server
        self.user = username
        self.pwd = password
        self._cache: dict[str, dict | None] = {}
        self.__cluster = None
        self.__bucket = None
        self.__coll_manifests = None
        self.__coll_objects = None
        self.__coll_object_data = None

        self._opts = ClusterOptions(
            authenticator=PasswordAuthenticator(self.user, self.pwd),
            timeout_options=ClusterTimeoutOptions(
                resolve_timeout=COUCHBASE_CONNECT_TIMEOUT,
                connect_timeout=COUCHBASE_CONNECT_TIMEOUT,
                bootstrap_timeout=COUCHBASE_CONNECT_TIMEOUT,
            ),
        )

    @property
    def _cluster(self) -> Cluster:
        if not self.__cluster:
            self.__cluster = Cluster(
                f"couchbase://{self.host}", self._opts
            )  # type: ignore
        return self.__cluster

    @property
    def _bucket(self) -> Bucket:
        if not self.__bucket:
            self.__bucket = self._cluster.bucket("clcache")
        return self.__bucket

    @property
    def _coll_manifests(self) -> Collection:
        if not self.__coll_manifests:
            self.__coll_manifests = self._bucket.collection("manifests")
        return self.__coll_manifests

    @property
    def _coll_objects(self) -> Collection:
        if not self.__coll_objects:
            self.__coll_objects = self._bucket.collection("objects")
        return self.__coll_objects

    @property
    def _coll_object_data(self) -> Collection:
        if not self.__coll_object_data:
            self.__coll_object_data = self._bucket.collection("objects_data")
        return self.__coll_object_data

    def get_unsynced_object_ids(self, not_from: str | None = None) -> set[str]:
        query = ""
        if not not_from:
            query = """
                SELECT META(m).id AS id
                FROM `clcache`.`_default`.`objects` AS m
                WHERE META(m).expiration > 0;"""
        else:
            query = f"""
                SELECT META(m).id AS id
                FROM `clcache`.`_default`.`objects` AS m
                WHERE META(m).expiration > 0
                    AND (m.`sync_source` IS MISSING
                        OR '{not_from}' NOT IN m.`sync_source`);
                """
        if not (result := self._cluster.query(query)):
            return set()

        rows = result.rows()

        # convert to set of id
        return {row["id"] for row in rows}

    @functools.cache
    def get_object(self, key: str) -> dict | None:
        res = self._coll_objects.get(key, GetOptions(timeout=COUCHBASE_ACCESS_TIMEOUT))  # type: ignore
        verify_success(res)

        payload = res.content_as[dict]

        if (
            "chunk_count" not in payload
            or "md5" not in payload
            or "stdout" not in payload
            or "stderr" not in payload
        ):
            return None

        chunk_count = payload["chunk_count"]
        obj_data = []
        hasher = HashAlgorithm()
        for i in range(1, chunk_count + 1):
            res = self._coll_object_data.get(
                f"{key}-{i}",
                GetOptions(
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
                res = self._coll_object_data.remove(
                    f"{key}-{i}", RemoveOptions(timeout=COUCHBASE_ACCESS_TIMEOUT)
                )  # type: ignore
                verify_success(res)

            return None

        payload["obj"] = b"".join(obj_data)
        return payload

    def delete_object(self, key: str) -> None:
        res = self._coll_objects.get(key, GetOptions(timeout=COUCHBASE_ACCESS_TIMEOUT))  # type: ignore
        if not res.success:
            return

        payload = res.content_as[dict]

        if "chunk_count" not in payload:
            return

        chunk_count = payload["chunk_count"]
        for i in range(1, chunk_count + 1):
            self._coll_object_data.remove(f"{key}-{i}", RemoveOptions(timeout=COUCHBASE_ACCESS_TIMEOUT))  # type: ignore

        self._coll_objects.remove(key)

    def set_object(self, key: str, payload: dict, sync_source: str) -> bool:
        obj_data = payload["obj"]
        obj_view = memoryview(obj_data)
        hasher = HashAlgorithm()
        hasher.update(obj_view)

        if hasher.hexdigest() != payload["md5"]:
            raise RuntimeError("Hash mismatch")

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
                    transcoder=RawBinaryTranscoderEx(), timeout=COUCHBASE_ACCESS_TIMEOUT
                ),  # type: ignore
            )
            verify_success(res)

            res = self._coll_object_data.touch(
                sub_key,
                COUCHBASE_EXPIRATION,
                TouchOptions(timeout=COUCHBASE_ACCESS_TIMEOUT),
            )  # type: ignore
            verify_success(res)

        sync_sources = payload.get("sync_source", [])
        if sync_source not in sync_sources:
            sync_sources.append(sync_source)

        payload = {
            "stdout": payload["stdout"],
            "stderr": payload["stderr"],
            "chunk_count": i,
            "md5": payload["md5"],
            "sync_source": sync_sources,
        }

        res = self._coll_objects.upsert(
            key, payload, UpsertOptions(timeout=COUCHBASE_ACCESS_TIMEOUT)
        )  # type: ignore
        verify_success(res)

        res = self._coll_objects.touch(
            key, COUCHBASE_EXPIRATION, timeout=COUCHBASE_ACCESS_TIMEOUT
        )  # type: ignore
        return res.success

    def set_manifest(self, key: str, manifest: Manifest) -> bool:
        """
        Set the manifest in the remote cache

        Important:
            Even though we attempt to merge the manifests, this is not atomic.
            Therefore, it is possible that another process will write a manifest
            between the time we fetch the existing manifest and the time we write
            the new manifest.

            Also, we are likely to write a manifest referencing object keys
            that do not exist in the remote cache. This is not a major issue, as
            the result is simply that retrieving the object will fail.
        """
        try:
            # First fetch existing manifest
            if remote_manifest := self.get_manifest(key):
                # Merge the manifests
                entries = list(set(remote_manifest.entries() + manifest.entries()))
                manifest = Manifest(entries)

            entries = [e._asdict() for e in manifest.entries()]
            json_object = {"entries": entries}
            if coll_manifests := self._coll_manifests:
                res = coll_manifests.upsert(
                    key, json_object, UpsertOptions(timeout=COUCHBASE_ACCESS_TIMEOUT)
                )  # type: ignore
                verify_success(res)
                res = coll_manifests.touch(key, COUCHBASE_EXPIRATION)
                verify_success(res)
            return True
        except Exception:
            return False

    @functools.cache
    def get_manifest(self, key: str) -> Manifest | None:
        with contextlib.suppress(Exception):
            res = self._coll_manifests.get(
                key, GetOptions(timeout=COUCHBASE_ACCESS_TIMEOUT)
            )  # type: ignore
            verify_success(res)
            return Manifest(
                [
                    ManifestEntry(
                        e["includeFiles"], e["includesContentHash"], e["objectHash"]
                    )
                    for e in res.content_as[dict]["entries"]
                ]
            )
        return None

    @functools.cache
    def get_manifest_by_object_hash(
        self, object_hash: str
    ) -> tuple[str, Manifest] | None:
        query = f"""
            SELECT META(m).id AS id
            FROM `clcache`.`_default`.`manifests` AS m
            WHERE ANY entry IN m.entries SATISFIES entry.objectHash = '{object_hash}' END
            LIMIT 1;
        """
        result = self._cluster.query(query)
        rows = list(result.rows())
        if not rows:
            return None

        manifest_id = rows[0]["id"]
        manifest = self.get_manifest(manifest_id)

        return None if manifest is None else (manifest_id, manifest)


class RawBinaryTranscoderEx(Transcoder):
    def encode_value(self, value: Union[bytes, bytearray]) -> tuple[bytes, int]:
        if not isinstance(value, (bytes, (bytearray, memoryview))):
            raise ValueFormatException(
                "Only binary data supported by RawBinaryTranscoder"
            )
        if isinstance(value, (bytearray, memoryview)):
            value = bytes(value)
        return value, FMT_BYTES

    def decode_value(self, value: bytes, flags: int) -> bytes:
        fmt = get_decode_format(flags)

        if fmt == FMT_BYTES:
            if isinstance(value, (bytearray, memoryview)):
                value = bytes(value)
            return value
        elif fmt == FMT_UTF8:
            raise ValueFormatException(
                "String format type not supported by RawBinaryTranscoder"
            )
        elif fmt == FMT_JSON:
            raise ValueFormatException(
                "JSON format type not supported by RawBinaryTranscoder"
            )
        else:
            raise RuntimeError("Unexpected flags value.")
