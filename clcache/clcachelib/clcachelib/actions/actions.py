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
from types import ModuleType

from clcachelib.cache.ex import LogicException
from clcachelib.config import VERSION
from clcachelib.utils.logging import log


def parse_args() -> argparse.Namespace | None:
    """
    Parse the command line arguments
    """

    parser = argparse.ArgumentParser(description=f"clcache.py v{VERSION}")

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
        help="Optional path to compiler executable.",
    )

    # Add remaining arguments
    parser.add_argument(
        "args",
        type=str,
        default=None,
        nargs=argparse.REMAINDER,
        help="Optional arguments for the compiler executable.",
    )

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    options, remainder = parser.parse_known_args(sys.argv[1:3])
    remainder.extend(options.args)

    # If there are no arguments, we are running a standalone command
    return options if not remainder and not options.compiler else None


def _find_compiler_binary() -> Path | None:
    if "CLCACHE_CL" in os.environ:
        path: Path = Path(os.environ["CLCACHE_CL"])

        # If the path is not absolute, try to find it in the PATH
        if path.name == path:
            if p := which(path):
                path = Path(p)

        return path if path is not None and path.exists() else None

    return Path(p) if (p := which("cl.exe")) else None


def handle_clcache_options(clcache_options: argparse.Namespace, cache) -> int | None:
    # sourcery skip: extract-duplicate-method
    from clcachelib.cache.cache import (
        clean_cache,
        clear_cache,
        print_statistics,
        reset_stats,
    )

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
            print("Max size argument must be greater than 0.", file=sys.stderr)
            return 1

        cache.configuration.set_max_cache_size(max_size_value)
        cache.configuration.save()
        print_statistics(cache)
        return 0

    if clcache_options.cache_size is not None:
        max_size_value = clcache_options.cache_size
        if max_size_value < 1:
            print("Max size argument must be greater than 0.", file=sys.stderr)
            return 1

        cache.configuration.set_max_cache_size(max_size_value)
        cache.configuration.save()
        print_statistics(cache)
        return 0


def _get_compiler_path_from_moccache_config(moccache_json: Path) -> Path | None:
    """
    Get the compiler path from the moccache_config.json file
    """
    if moccache_json.is_file():
        # read compiler path from moccache.json
        import json

        with open(moccache_json) as f:
            config = json.load(f)
            return Path(config["moc_path"])
    return None


def get_compiler_path(build_dir: Path) -> tuple[Path, ModuleType, list[str]]:

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
    self = Path(sys.argv[0])
    if self.name.lower() == "__main__.py":
        identity = self.parent.name.lower()
    else:
        identity = self.stem.lower()

    if compiler_path and compiler_path.name.lower() == "moc.exe":
        identity = "moccache"

    if identity == "clcache":
        compiler_pkg = __import__("clcachelib.cl.compiler", fromlist=["compiler"])

        # parse the config file
        _read_config_file(build_dir, "clcache_config.json")

        if compiler_path is None:
            compiler_path = _find_compiler_binary()

    elif identity == "moccache":
        compiler_pkg = __import__("clcachelib.moc.compiler", fromlist=["compiler"])

        # parse the config file
        if moccache_config := _read_config_file(build_dir, "moccache_config.json"):
            if compiler_path is None:
                compiler_path = _get_compiler_path_from_moccache_config(moccache_config)

    else:
        raise LogicException(f"Unknown compiler identity: {identity!s}")

    if not (compiler_path and compiler_path.exists()):
        raise LogicException(
            "Failed to locate specified compiler, or exe on PATH (and CLCACHE_CL is not set), aborting."
        )

    log(f"Compiler binary: {compiler_path}")
    return compiler_path, compiler_pkg, args


def _read_config_file(build_dir: Path, file_name: str) -> Path | None:
    """
    Read the config file (clcache_config.json or moccache_config.json) and parse environment entries

    Returns:
        Path to config file, or None if not found
    """

    config_json = build_dir / file_name
    # test if exists and is readable
    if not config_json.is_file():
        return None
    
    import json
    with open(config_json) as f:
        config = json.load(f)

        # Iterate over "env" key containing dictionary of environment variables
        for key, value in config.get("env", {}).items():
            os.environ[key] = value
    return config_json
