#!/usr/bin/env python
#
# This file is part of the clcache project.
#
# The contents of this file are subject to the BSD 3-Clause License, the
# full text of which is available in the accompanying LICENSE file at the
# root directory of this project.
#
import time
from clcache_lib.config import VERSION
import argparse
import os
from pathlib import Path
import sys
from typing import Optional


def parse_args() -> argparse.Namespace:
    '''Parse the command line arguments'''
    class CommandCheckAction(argparse.Action):
        def __call__(self, parser, namespace, values, optional_string=None):
            if values and not values.lower().endswith(".exe"):
                setattr(namespace, "non_command", values)
                return
            setattr(namespace, self.dest, values)

    class RemainderSetAction(argparse.Action):
        def __call__(self, parser, namespace, values, optional_string=None):
            if nonCommand := getattr(namespace, "non_command", None):
                values.insert(0, nonCommand)
            setattr(namespace, self.dest, values)

    parser = argparse.ArgumentParser(description=f"clcache.py v{VERSION}")
    # Handle the clcache standalone actions, only one can be used at a time
    group_parser = parser.add_mutually_exclusive_group()
    group_parser.add_argument(
        "-s",
        "--stats",
        dest="show_stats",
        action="store_true",
        help="print cache statistics",
    )
    group_parser.add_argument(
        "-c", "--clean", dest="clean_cache", action="store_true", help="clean cache"
    )
    group_parser.add_argument(
        "-C", "--clear", dest="clear_cache", action="store_true", help="clear cache"
    )
    group_parser.add_argument(
        "-z",
        "--reset",
        dest="reset_stats",
        action="store_true",
        help="reset cache statistics",
    )
    group_parser.add_argument(
        "-M",
        "--set-size",
        dest="cache_size",
        type=int,
        default=None,
        help="set maximum cache size (in bytes)",
    )
    group_parser.add_argument(
        "--set-size-gb",
        dest="cache_size_gb",
        type=int,
        default=None,
        help="set maximum cache size (in GB)",
    )
    group_parser.add_argument(
        "--run-server",
        dest="run_server",
        type=int,
        default=None,
        help="run clcache server (optional timeout in seconds)",
    )

    # This argument need to be optional, or it will be required for the status commands above
    parser.add_argument(
        "compiler",
        default=None,
        action=CommandCheckAction,
        nargs="?",
        help="Optional path to compile executable. If not "
        "present look in CLCACHE_CL environment variable "
        "or search PATH for exe.",
    )
    parser.add_argument(
        "compiler_args",
        action=RemainderSetAction,
        nargs=argparse.REMAINDER,
        help="Arguments to the compiler",
    )

    return parser.parse_args()


def main() -> int:  # sourcery skip: de-morgan, extract-duplicate-method
    
    options = parse_args()

    if options.run_server is not None:
        # Run clcache server
        from clcache_lib.cache.server import PipeServer

        if PipeServer.is_running():
            return 0

        # we are the first instance
        server = PipeServer(timeout_s=options.run_server)
        return server.run()

    from clcache_lib.cache.cache import Cache, clean_cache, clear_cache, print_statistics, reset_stats
    from clcache_lib.cache.ex import LogicException
    from clcache_lib.cache.virt import set_llvm_dir
    from clcache_lib.cl.compiler import invoke_real_compiler
    from clcache_lib.clcache import process_compile_request
    from clcache_lib.utils.util import find_compiler_binary, trace

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

        compiler: Optional[Path] = options.compiler or find_compiler_binary()
        if not (compiler and os.access(compiler, os.F_OK)):
            print(
                "Failed to locate specified compiler, or exe on PATH (and CLCACHE_CL is not set), aborting."
            )
            return 1

        # Extract the compiler folder from the compiler path
        set_llvm_dir(compiler)

        trace("Found real compiler binary at '{0!s}'".format(compiler))
        trace(f"Arguments we care about: '{sys.argv}'")

        # Determine CL_

        if "CLCACHE_DISABLE" in os.environ:
            return invoke_real_compiler(compiler, options.compiler_args)[0]
        try:
            return process_compile_request(cache, compiler, options.compiler_args)
        except LogicException as e:
            print(e)
            return 1


if __name__ == "__main__":
    # calculate execution time
    now = time.time()
    result = main()
    
    print(f"Execution time: {time.time() - now:.2f} seconds")
    sys.exit(result)
