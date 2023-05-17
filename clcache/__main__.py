#!/usr/bin/env python
#
# This file is part of the clcache project.
#
# The contents of this file are subject to the BSD 3-Clause License, the
# full text of which is available in the accompanying LICENSE file at the
# root directory of this project.
#
import os
import sys

from clcache_lib.actions import (get_compiler_path, handle_clcache_options,
                                 parse_args)
from clcache_lib.utils.logging import LogLevel, flush_logger, init_logger, log
from clcache_lib.utils.util import get_build_dir
from clcache_lib.cache.cache import Cache


def main() -> int:
    if "CLCACHE_ACCESS_VIOLATION" in os.environ:
        log("Recovering from access violation", LogLevel.ERROR)

    clcache_options = parse_args()

    with Cache() as cache:

        if clcache_options is not None:
            exit_code = handle_clcache_options(clcache_options, cache)
            if exit_code is not None:
                return exit_code

        compiler_path, compiler_pkg, args = get_compiler_path()

        if "CLCACHE_DISABLE" in os.environ:
            return compiler_pkg.invoke_real_compiler(compiler_path, args)[0]

        return compiler_pkg.process_compile_request(cache, compiler_path, args)


if __name__ == "__main__":
    try:
        # get build folder
        build_dir = get_build_dir()

        # initialize logger if environment variable is set
        if "CLCACHE_DISABLE_LOGGING" not in os.environ:
            init_logger(build_dir)

        sys.exit(main())
    except Exception as e:
        # Log exception with full traceback
        import traceback
        log("Exception: {!s}".format(
            traceback.format_exc()), level=LogLevel.ERROR, force_flush=True)

        sys.exit(1)
    finally:
        flush_logger()
