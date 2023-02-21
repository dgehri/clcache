
from datetime import timedelta

VERSION = "4.3.6-dgehri"
CACHE_VERSION = "5"

COUCHBASE_EXPIRATION = timedelta(days=3)
COUCHBASE_CONNECT_TIMEOUT = timedelta(seconds=1)
COUCHBASE_GET_TIMEOUT = timedelta(seconds=4)


# The codec that is used by clcache to store compiler STDOUR and STDERR in
# output.txt and stderr.txt.
# This codec is up to us and only used for clcache internal storage.
# For possible values see https://docs.python.org/2/library/codecs.html
CACHE_COMPILER_OUTPUT_STORAGE_CODEC = "utf-8"

# The cl default codec
CL_DEFAULT_CODEC = "mbcs"

# Manifest file will have at most this number of hash lists in it. Need to avoi
# manifests grow too large.
MAX_MANIFEST_HASHES = 100
