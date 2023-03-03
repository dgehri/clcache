
from datetime import timedelta

VERSION = "4.4.0c-dgehri"
CACHE_VERSION = "5"

COUCHBASE_EXPIRATION = timedelta(days=3)
COUCHBASE_CONNECT_TIMEOUT = timedelta(seconds=1)
COUCHBASE_GET_TIMEOUT = timedelta(seconds=4)

# The cl default codec
CL_DEFAULT_CODEC = "mbcs"

# Manifest file will have at most this number of hash lists in it. Need to avoi
# manifests grow too large.
MAX_MANIFEST_HASHES = 100
