#!/usr/bin/env python
#
# This file is part of the clcache project.
#
# The contents of this file are subject to the BSD 3-Clause License, the
# full text of which is available in the accompanying LICENSE file at the
# root directory of this project.
#
import argparse
import os
from shutil import which
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from clcache_lib.config import VERSION


def _parse_args() -> Tuple[argparse.Namespace, List[str]]:
    '''Parse the command line arguments'''

    parser = argparse.ArgumentParser(
        description=f"clcache.py v{VERSION}", fromfile_prefix_chars="@")

    # Handle the clcache standalone actions, only one can be used at a time
    cmd_group = parser.add_mutually_exclusive_group()
    cmd_group.add_argument(
        "-s",
        "--stats",
        dest="show_stats",
        action="store_true",
        help="Print cache statistics",
    )
    cmd_group.add_argument(
        "-c", "--clean", dest="clean_cache", action="store_true", help="Clean cache"
    )
    cmd_group.add_argument(
        "-C", "--clear", dest="clear_cache", action="store_true", help="Clear cache"
    )
    cmd_group.add_argument(
        "-z",
        "--reset",
        dest="reset_stats",
        action="store_true",
        help="Reset cache statistics",
    )
    cmd_group.add_argument(
        "-M",
        "--set-size",
        dest="cache_size",
        type=int,
        default=None,
        help="Set maximum cache size (in bytes)",
    )
    cmd_group.add_argument(
        "--set-size-gb",
        dest="cache_size_gb",
        type=int,
        default=None,
        help="Set maximum cache size (in GB)",
    )
    cmd_group.add_argument(
        "--run-server",
        dest="run_server",
        type=int,
        default=None,
        help="Run clcache server (optional timeout in seconds)",
    )

    parser.add_argument(
        "--compiler-executable",
        dest="compiler",
        type=str,
        default=None,
        nargs="?",
        help="Optional path to compiler executable.",
    )

    options, remainder = parser.parse_known_args()

    if (
        options.compiler is None
        and len(remainder) > 0
        and not remainder[0].startswith(("-", "/"))
        and remainder[0].endswith(".exe")
    ):
        options.compiler = remainder[0]
        remainder = remainder[1:]

    return options, remainder


def _find_compiler_binary() -> Optional[Path]:
    if "CLCACHE_CL" in os.environ:
        path: Path = Path(os.environ["CLCACHE_CL"])

        # If the path is not absolute, try to find it in the PATH
        if path.name == path:
            if p := which(path):
                path = Path(p)

        return path if path is not None and path.exists() else None

    return Path(p) if (p := which("cl.exe")) else None


def main() -> int:  # sourcery skip: de-morgan, extract-duplicate-method

    options, compiler_args = _parse_args()

    if options.run_server is not None:
        # Run clcache server
        from clcache_lib.cache.server import PipeServer

        if PipeServer.is_running():
            return 0

        # we are the first instance
        server = PipeServer(timeout_s=options.run_server)
        return server.run()

    from clcache_lib.cache.cache import (Cache, clean_cache, clear_cache,
                                         print_statistics, reset_stats)

    with Cache() as cache:

        if options.show_stats:
            print_statistics(cache)
            return 0

        if options.clean_cache:
            clean_cache(cache)
            print("Cache cleaned")
            return 0

        if options.clear_cache:
            clear_cache(cache)
            print("Cache cleared")
            print_statistics(cache)
            return 0

        if options.reset_stats:
            reset_stats(cache)
            print("Statistics reset")
            print_statistics(cache)
            return 0

        if options.cache_size_gb is not None:
            max_size_value = options.cache_size_gb * 1024 * 1024 * 1024
            if max_size_value < 1:
                print("Max size argument must be greater than 0.", file=sys.stderr)
                return 1

            cache.configuration.set_max_cache_size(max_size_value)
            print_statistics(cache)
            return 0

        if options.cache_size is not None:
            max_size_value = options.cache_size
            if max_size_value < 1:
                print("Max size argument must be greater than 0.", file=sys.stderr)
                return 1

            cache.configuration.set_max_cache_size(max_size_value)
            print_statistics(cache)
            return 0

        compiler_path = None
        if options.compiler:
            compiler_path = Path(options.compiler)
        else:
            compiler_path = _find_compiler_binary()

        if not (compiler_path and compiler_path.exists()):
            print(
                "Failed to locate specified compiler, or exe on PATH (and CLCACHE_CL is not set), aborting."
            )
            return 1

        if compiler_path.name.lower() == "moc.exe":
            import clcache_lib.moc.compiler as compiler
        else:
            import clcache_lib.cl.compiler as compiler

        from clcache_lib.cache.ex import LogicException
        from clcache_lib.utils.util import trace

        trace("Found real compiler binary at '{0!s}'".format(compiler_path))

        if "CLCACHE_DISABLE" in os.environ:
            return compiler.invoke_real_compiler(compiler_path, compiler_args)[0]

        try:
            return compiler.process_compile_request(cache, compiler_path, compiler_args)
        except LogicException as e:
            print(e)
            return 1


if __name__ == "__main__":
    sys.exit(main())
