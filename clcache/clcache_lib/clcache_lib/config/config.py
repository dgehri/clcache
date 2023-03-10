
from datetime import timedelta

VERSION = "4.4.0q-dgehri"
CACHE_VERSION = "7"

COUCHBASE_EXPIRATION = timedelta(days=3)
COUCHBASE_CONNECT_TIMEOUT = timedelta(seconds=1)
COUCHBASE_GET_TIMEOUT = timedelta(seconds=4)

# The cl default codec
CL_DEFAULT_CODEC = "mbcs"

# Manifest file will have at most this number of hash lists in it. Need to avoid
# manifests grow too large.
MAX_MANIFEST_HASHES = 100

# Maximum idle time for a cache server before it shuts down
HASH_SERVER_TIMEOUT = timedelta(seconds=60)
