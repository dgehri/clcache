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
import sys
from pathlib import Path
from shutil import which
import time
from types import ModuleType
from typing import List, Optional, Tuple

from clcache_lib.cache.ex import LogicException
from clcache_lib.config import VERSION
from clcache_lib.utils.file_lock import FileLock
from clcache_lib.utils.logging import LogLevel, flush_logger, init_logger, log
from clcache_lib.utils.util import get_build_dir


def _parse_args() -> Optional[argparse.Namespace]:
    '''
    Parse the command line arguments
    '''

    parser = argparse.ArgumentParser(
        description=f"clcache.py v{VERSION}")

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

    # Add positional arguments for the compiler executable
    cmd_group.add_argument(
        "compiler",
        type=str,
        default=None,
        nargs="?",
        help="Optional path to compiler executable."
    )

    # Add remaining arguments
    parser.add_argument(
        "args",
        type=str,
        default=None,
        nargs=argparse.REMAINDER,
        help="Optional arguments for the compiler executable."
    )

    options, remainder = parser.parse_known_args(sys.argv[1:3])
    remainder.extend(options.args)

    # If there are no arguments, we are running a standalone command
    return options if not remainder and not options.compiler else None


def _find_compiler_binary() -> Optional[Path]:
    if "CLCACHE_CL" in os.environ:
        path: Path = Path(os.environ["CLCACHE_CL"])

        # If the path is not absolute, try to find it in the PATH
        if path.name == path:
            if p := which(path):
                path = Path(p)

        return path if path is not None and path.exists() else None

    return Path(p) if (p := which("cl.exe")) else None


def _handle_clcache_options(clcache_options: argparse.Namespace, cache) -> Optional[int]:
    # sourcery skip: extract-duplicate-method
    from clcache_lib.cache.cache import (clean_cache, clear_cache,
                                         print_statistics, reset_stats)

    if clcache_options.show_stats:
        print_statistics(cache)
        return 0

    if clcache_options.clean_cache:
        clean_cache(cache)
        print("Cache cleaned")
        return 0

    if clcache_options.clear_cache:
        clear_cache(cache)
        print("Cache cleared")
        print_statistics(cache)
        return 0

    if clcache_options.reset_stats:
        reset_stats(cache)
        print("Statistics reset")
        print_statistics(cache)
        return 0

    if clcache_options.cache_size_gb is not None:
        max_size_value = clcache_options.cache_size_gb * 1024 * 1024 * 1024
        if max_size_value < 1:
            print("Max size argument must be greater than 0.",
                  file=sys.stderr)
            return 1

        cache.configuration.set_max_cache_size(max_size_value)
        print_statistics(cache)
        return 0

    if clcache_options.cache_size is not None:
        max_size_value = clcache_options.cache_size
        if max_size_value < 1:
            print("Max size argument must be greater than 0.",
                  file=sys.stderr)
            return 1

        cache.configuration.set_max_cache_size(max_size_value)
        print_statistics(cache)
        return 0


def _get_compiler_path() -> Tuple[Path, ModuleType, List[str]]:

    # Clone arguments from sys.argv
    args = sys.argv[1:].copy()

    # Get the first argument of the command line and check if it is a compiler
    compiler_path = None
    if (
        len(args) > 0
        and not args[0].startswith(("-", "/"))
        and args[0].endswith(".exe")
    ):
        compiler_path = Path(args.pop(0))

    # Find out if we are running as clcache or moccache
    identity = Path(sys.argv[0]).stem.lower()
    if compiler_path and compiler_path.name.lower() == "moc.exe":
        identity = "moccache"

    if identity == "clcache":
        compiler_pkg = __import__(
            "clcache_lib.cl.compiler", fromlist=["compiler"])

        if compiler_path is None:
            compiler_path = _find_compiler_binary()

    elif identity == "moccache":
        compiler_pkg = __import__(
            "clcache_lib.moc.compiler", fromlist=["compiler"])

        if compiler_path is None:
            # Locate "moccache.json" in current directory and parent directories, next to "CMakeCache.txt"
            for path in [Path.cwd()] + list(Path.cwd().parents):
                if (path / "CMakeCache.txt").exists():
                    moccache_json = path / "moccache_config.json"
                    # test if exists and is readable
                    if moccache_json.exists() and moccache_json.is_file():
                        # read compiler path from moccache.json
                        import json
                        with open(moccache_json, "r") as f:
                            config = json.load(f)
                            compiler_path = Path(
                                config["moc_path"])
                    break
                path = path.parent

    else:
        raise LogicException(
            "Unknown compiler identity: {0!s}".format(identity))

    if not (compiler_path and compiler_path.exists()):
        raise LogicException(
            "Failed to locate specified compiler, or exe on PATH (and CLCACHE_CL is not set), aborting."
        )

    log(f"Found real compiler binary at {compiler_path}")
    return compiler_path, compiler_pkg, args


def main() -> int:
    clcache_options = _parse_args()
    if clcache_options is not None and clcache_options.run_server is not None:
        # Run clcache server
        from clcache_lib.cache.server import PipeServer

        if PipeServer.is_running():
            return 0

        # we are the first instance
        server = PipeServer(timeout_s=clcache_options.run_server)
        return server.run()

    from clcache_lib.cache.cache import Cache

    with Cache() as cache:

        if clcache_options is not None:
            exit_code = _handle_clcache_options(clcache_options, cache)
            if exit_code is not None:
                return exit_code

        compiler_path, compiler_pkg, args = _get_compiler_path()

        if "CLCACHE_DISABLE" in os.environ:
            return compiler_pkg.invoke_real_compiler(compiler_path, args)[0]

        return compiler_pkg.process_compile_request(cache, compiler_path, args)


if __name__ == "__main__":
    try:
        # get build folder
        init_logger(get_build_dir())
        sys.exit(main())
    except Exception as e:
        # Print exception with full traceback
        import traceback
        traceback.print_exc()

        # Also try to log
        log("Exception: {0!s}".format(
            traceback.format_exc()), level=LogLevel.ERROR)
        sys.exit(1)
    finally:
        flush_logger()
