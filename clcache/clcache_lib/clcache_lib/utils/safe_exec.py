import os
import subprocess
import sys

from ..utils.logging import LogLevel, log

STATUS_ACCESS_VIOLATION = 0xC0000005


def safe_execute(func):
    def _exec(*args):
        args = []
        if "__compiled__" not in globals():
            args.append(sys.executable)
        args.extend(sys.argv)

        return subprocess.call(args)

    def wrapper(*args, **kwargs):
        if (
            "CLCACHE_NO_SAFE_EXECUTE" in os.environ
            or "CLCACHE_COUCHBASE" not in os.environ
        ):
            return func(*args, **kwargs)

        os.environ["CLCACHE_NO_SAFE_EXECUTE"] = "1"

        # first try executing as-is
        result = _exec(*args)

        if result == STATUS_ACCESS_VIOLATION:
            # if we get an access violation, try again without CLCACHE_COUCHBASE
            log("Access violation detected, retrying without CLCACHE_COUCHBASE",
                level=LogLevel.ERROR)
            os.environ.pop("CLCACHE_COUCHBASE", None)
            result = _exec(*args)

        return result

    return wrapper
