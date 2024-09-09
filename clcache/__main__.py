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
from pathlib import Path

lib_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "clcachelib")
sys.path.insert(0, lib_path)

from clcachelib.actions import get_compiler_path, handle_clcache_options, parse_args
from clcachelib.utils.logging import LogLevel, flush_logger, init_logger, log
from clcachelib.utils.util import get_build_dir
from clcachelib.cache.cache import Cache


def main(build_dir: Path) -> int:
    if "CLCACHE_ACCESS_VIOLATION" in os.environ:
        log("Recovering from access violation", LogLevel.ERROR)

    clcache_options = parse_args()

    with Cache() as cache:
        if clcache_options is not None:
            exit_code = handle_clcache_options(clcache_options, cache)
            if exit_code is not None:
                return exit_code

        compiler_path, compiler_pkg, args = get_compiler_path(build_dir)

        if compiler_pkg.is_disabled():
            return compiler_pkg._invoke_real_compiler(
                compiler_path, args, disable_auto_rsp=True
            )

        return compiler_pkg.process_compile_request(cache, compiler_path, args)


if __name__ == "__main__":
    # get build folder
    build_dir = get_build_dir()

    try:
        # initialize logger if environment variable is set
        if "CLCACHE_DISABLE_LOGGING" not in os.environ:
            init_logger(build_dir)

        sys.exit(main(build_dir))
    except Exception as e:
        # Log exception with full traceback
        import traceback

        log(
            "Exception: {!s}".format(traceback.format_exc()),
            level=LogLevel.ERROR,
            force_flush=True,
        )

        # Fall back to original compiler
        compiler_path, compiler_pkg, args = get_compiler_path(build_dir)
        sys.exit(
            compiler_pkg._invoke_real_compiler(
                compiler_path, args, disable_auto_rsp=True
            )
        )
    finally:
        flush_logger()
