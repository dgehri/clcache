
from datetime import timedelta

VERSION = "4.4.3j-dgehri"
CACHE_VERSION = "9"

COUCHBASE_EXPIRATION = timedelta(days=3)
COUCHBASE_CONNECT_TIMEOUT = timedelta(seconds=1)
COUCHBASE_GET_TIMEOUT = timedelta(seconds=4)

# The cl default codec
CL_DEFAULT_CODEC = "mbcs"

# Manifest file will have at most this number of hash lists in it. Need to avoid
# manifests grow too large.
MAX_MANIFEST_HASHES = 100

# Maximum idle time for cache server before it shuts down
#
# This value can be overridden by setting the CLCACHE_SERVER_TIMEOUT_MINUTES
# environment variable to a value in minutes.
# 
# Use of the cache server can be disabled entirely by setting the environment
# variable CLCACHE_SERVER_TIMEOUT_MINUTES to 0.
HASH_SERVER_TIMEOUT = timedelta(seconds=180)
