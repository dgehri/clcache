#!/usr/bin/env python
#
# This file is part of the clcache project.
#
# The contents of this file are subject to the BSD 3-Clause License, the
# full text of which is available in the accompanying LICENSE file at the
# root directory of this project.
#
import argparse
import concurrent.futures
import contextlib
import cProfile
import os
import re
import sys
import time
from typing import Iterator, List, Set, Tuple

from clcache_lib.utils import *  # type: ignore
from clcache_lib.cache import *  # type: ignore
from clcache_lib.cl import *  # type: ignore
from clcache_lib.config import VERSION


def parse_includes_set(compiler_output: str, src_file: Path, strip: bool) -> Tuple[List[Path], str]:
    """
    Parse the compiler output and return a set of include file paths.

        Parameters:
            compiler_output: The compiler output to parse.
            src_file: The source file that was compiled.
            strip: If True, remove all lines with include directives from the output.

        Returns:
            A tuple of a set of include file paths and the compiler output with or without include directives.
    """
    filtered_output = []
    include_set: Set[Path] = set()

    # Example lines
    # Note: including file:         C:\Program Files (x86)\Microsoft Visual Studio 12.0\VC\INCLUDE\limits.h
    # Hinweis: Einlesen der Datei:   C:\Program Files (x86)\Microsoft Visual Studio 12.0\VC\INCLUDE\iterator
    #
    # So we match
    # - one word (translation of "note")
    # - colon
    # - space
    # - a phrase containing characters and spaces (translation of "including file")
    # - colon
    # - one or more spaces
    # - the file path, starting with a non-whitespace character
    regex = re.compile(r"^(\w+): ([ \w]+):( +)(?P<file_path>\S.*)$")

    abs_src_file = src_file.absolute()
    for line in line_iter(compiler_output):
        if m := regex.match(line.rstrip("\r\n")):
            file_path = Path(os.path.normpath(m["file_path"])).absolute()
            if file_path != abs_src_file:
                include_set.add(file_path)
        elif strip:
            filtered_output.append(line)
    if strip:
        return list(include_set), "".join(filtered_output)
    else:
        return list(include_set), compiler_output


def process_cache_hit(cache: Cache, is_local: bool, obj_file: Path, cache_key: str) -> Tuple[int, str, str]:
    """
    Process a cache hit, copying the object file from the cache to the output directory.

        Parameters:
            cache: The cache to use.
            is_local: True if the cache is local, False if it is remote.
            obj_file: The object file to write.
            cache_key: The cache key to use.

        Returns:
            A tuple of the exit code, the stdout, the stderr
    """
    trace(
        f"Reusing cached object for key {cache_key} for object file {obj_file}")

    with cache.lock_for(cache_key):
        if is_local:
            cache.statistics.record_cache_hit(HitReason.LOCAL_CACHE_HIT)
        else:
            cache.statistics.record_cache_hit(HitReason.REMOTE_CACHE_HIT)

        if obj_file.exists():
            success = False
            for _ in range(60):
                try:
                    obj_file.unlink()
                    success = True
                    break
                except Exception:
                    time.sleep(1)

            if not success:
                obj_file.unlink()

        cached_artifacts = cache.get_entry(cache_key)
        assert cached_artifacts is not None

        copy_from_cache(cached_artifacts.obj_file_path, obj_file)
        trace("Finished. Exit code 0")
        return (
            0,
            expand_compile_output(cached_artifacts.stdout, StdStream.STDOUT),
            expand_compile_output(cached_artifacts.stderr, StdStream.STDERR),
        )


def create_manifest_entry(manifest_hash: str, include_paths: List[Path]) -> ManifestEntry:
    """
    Create a manifest entry for the given manifest hash and include paths.
    """

    sorted_include_paths = sorted(set(include_paths))
    include_hashes = get_file_hashes(sorted_include_paths)

    safe_includes = [canonicalize_path(path) for path in sorted_include_paths]
    content_hash = ManifestRepository.get_includes_content_hash_for_hashes(
        include_hashes
    )
    cachekey = CompilerArtifactsRepository.compute_key(
        manifest_hash, content_hash
    )

    return ManifestEntry(safe_includes, content_hash, cachekey)


def process_compile_request(cache: Cache, compiler: Path, args: List[str]) -> int:
    '''
    Process a compile request.

    Returns:
        The exit code of the compiler.
    '''
    trace("Parsing given commandline '{0!s}'".format(args))

    cmdline, environment = extend_cmdline_from_env(args, dict(os.environ))
    cmdline = expand_response_file(cmdline)
    trace("Expanded commandline '{0!s}'".format(cmdline))

    try:
        src_files, obj_files = CommandLineAnalyzer.analyze(cmdline)
        return schedule_jobs(
            cache, compiler, cmdline, environment, src_files, obj_files
        )
    except InvalidArgumentError:
        trace(f"Cannot cache invocation as {cmdline}: invalid argument")
        cache.statistics.record_cache_miss(
            MissReason.CALL_WITH_INVALID_ARGUMENT)
    except NoSourceFileError:
        trace(f"Cannot cache invocation as {cmdline}: no source file found")
        cache.statistics.record_cache_miss(MissReason.CALL_WITHOUT_SOURCE_FILE)
    except MultipleSourceFilesComplexError:
        trace(
            f"Cannot cache invocation as {cmdline}: multiple source files found")
        cache.statistics.record_cache_miss(
            MissReason.CALL_WITH_MULTIPLE_SOURCE_FILES)
    except CalledWithPchError:
        trace(
            f"Cannot cache invocation as {cmdline}: precompiled headers in use")
        cache.statistics.record_cache_miss(MissReason.CALL_WITH_PCH)
    except CalledForLinkError:
        trace(f"Cannot cache invocation as {cmdline}: called for linking")
        cache.statistics.record_cache_miss(MissReason.CALL_FOR_LINKING)
    except ExternalDebugInfoError:
        trace(
            f"Cannot cache invocation as {cmdline}: external debug information (/Zi) is not supported"
        )
        cache.statistics.record_cache_miss(
            MissReason.CALL_FOR_EXTERNAL_DEBUG_INFO)
    except CalledForPreprocessingError:
        trace(
            f"Cannot cache invocation as {cmdline}: called for preprocessing")
        cache.statistics.record_cache_miss(MissReason.CALL_FOR_PREPROCESSING)

    exit_code, out, err = invoke_real_compiler(compiler, args)
    print_stdout_and_stderr(out, err)
    return exit_code


def filter_source_files(
    cmd_line: List[str], src_files: List[Tuple[Path, str]]
) -> Iterator[str]:
    '''
    Filter out all source files from the command line

        Parameters:
            cmd_line: The command line to filter.
            src_files: The source files to filter.

        Returns:
            An iterator over the filtered command line.
    '''
    set_of_sources = {str(src_file) for src_file, _ in src_files}
    skipped_args = ("/Tc", "/Tp", "-Tp", "-Tc")
    yield from (
        arg
        for arg in cmd_line
        if not (arg in set_of_sources or arg.startswith(skipped_args))
    )


def schedule_jobs(
    cache: Cache,
    compiler: Path,
    cmd_line: List[str],
    environment: Dict[str, str],
    src_files: List[Tuple[Path, str]],
    obj_files: List[Path],
) -> int:
    '''
    Schedule jobs for the given command line.
    '''
    # Filter out all source files from the command line to form base_cmdline
    base_cmdline = [
        arg
        for arg in filter_source_files(cmd_line, src_files)
        if not arg.startswith("/MP")
    ]

    exit_code: int = 0

    if (len(src_files) == 1 and len(obj_files) == 1) or os.getenv(
        "CLCACHE_SINGLEFILE"
    ):
        assert len(src_files) == 1
        assert len(obj_files) == 1
        src_file, src_language = src_files[0]
        obj_file = obj_files[0]
        job_cmdline = base_cmdline + [src_language + str(src_file)]
        exit_code, out, err = process_single_source(
            cache, compiler, job_cmdline, src_file, obj_file, environment
        )
        trace("Finished. Exit code {0:d}".format(exit_code))
        print_stdout_and_stderr(out, err)
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=job_count(cmd_line)
        ) as executor:
            jobs = []
            for (src_file, src_language), obj_file in zip(src_files, obj_files):
                job_cmdline = base_cmdline + [src_language + str(src_file)]
                jobs.append(
                    executor.submit(
                        process_single_source,
                        cache,
                        compiler,
                        job_cmdline,
                        src_file,
                        obj_file,
                        environment,
                    )
                )
            for future in concurrent.futures.as_completed(jobs):
                exit_code, out, err = future.result()
                trace("Finished. Exit code {0:d}".format(exit_code))
                print_stdout_and_stderr(out, err)

                if exit_code != 0:
                    break

    return exit_code


def process_single_source(cache, compiler,
                          cmdline, src_file,
                          obj_file, environment: Optional[Dict[str, str]]) \
        -> Tuple[int, str, str]:
    '''
    Process a single source file.

        Parameters:
            cache (Cache): The cache to use.
            compiler (Path): The path to the compiler.
            cmdline (List[str]): The command line to invoke the compiler with.
            src_file (Path): The source file to compile.
            obj_file (Path): The object file to create.
            environment (Dict[str, str]): The environment to use when invoking the compiler.

        Returns:
            Tuple[int, str, str]: A tuple containing the exit code, stdout, stderr
    '''
    try:
        assert obj_file is not None
        return process(cache, obj_file, compiler, cmdline, src_file)

    except IncludeNotFoundException:
        return invoke_real_compiler(compiler, cmdline, environment=environment)
    except OSError:
        return invoke_real_compiler(compiler, cmdline, environment=environment)
    except CompilerFailedException as e:
        return e.getReturnTuple()
    except CacheLockException as e:
        trace(repr(e))
        return invoke_real_compiler(compiler, cmdline, environment=environment)


def process(cache: Cache, obj_file: Path, compiler: Path, cmdline: List[str], src_file: Path) -> Tuple[int, str, str]:
    '''
    Process a single source file.

        Parameters:
            cache (Cache): The cache to use.
            obj_file (Path): The object file to create.
            compiler (Path): The path to the compiler.
            src_file (Path): The source file to compile.

        Returns:
            Tuple[int, str, str, bool]: A tuple containing the exit code, stdout, stderr

    '''
    manifest_hash: str = ManifestRepository.get_manifest_hash(
        compiler, cmdline, src_file)
    manifest_hit = None
    cachekey = None
    with cache.manifest_lock_for(manifest_hash):
        if manifest := cache.get_manifest(manifest_hash):
            for entry_index, entry in enumerate(manifest.entries()):
                # NOTE: command line options already included in hash for manifest name
                with contextlib.suppress(IncludeNotFoundException):
                    includesContentHash = (
                        ManifestRepository.get_includes_content_hash_for_files(
                            [expand_path(path) for path in entry.includeFiles]
                        )
                    )

                    if entry.includesContentHash == includesContentHash:
                        cachekey = entry.objectHash
                        assert cachekey is not None
                        if entry_index > 0:
                            # Move manifest entry to the top of the entries in the manifest
                            manifest.touch_entry(cachekey)
                            cache.set_manifest(manifest_hash, manifest)

                        manifest_hit = True
                        with cache.lock_for(cachekey):
                            hit, is_local = cache.has_entry(cachekey)
                            if hit:
                                return process_cache_hit(cache, is_local, obj_file, cachekey)

            miss_reason = MissReason.HEADER_CHANGED_MISS
        else:
            miss_reason = MissReason.SOURCE_CHANGED_MISS

    strip_includes = None
    if manifest_hit is None:
        strip_includes = False
        if "/showIncludes" not in cmdline:
            cmdline = list(cmdline)
            cmdline.insert(0, "/showIncludes")
            strip_includes = True

    compiler_result: Tuple[int, str, str] = invoke_real_compiler(
        compiler, cmdline, capture_output=True)

    include_paths: List[Path] = []

    if manifest_hit is None and strip_includes is not None:
        include_paths, compiler_output = parse_includes_set(
            compiler_result[1], src_file, strip_includes
        )
        compiler_result = (
            compiler_result[0], compiler_output, compiler_result[2])

    with cache.manifest_lock_for(manifest_hash):
        if manifest_hit is not None and cachekey is not None:
            return ensure_artifacts_exist(
                cache, cachekey, miss_reason, obj_file, compiler_result
            )

        entry = create_manifest_entry(manifest_hash, include_paths)
        cachekey = entry.objectHash

        def add_manifest():
            manifest = cache.get_manifest(manifest_hash) or Manifest()
            manifest.add_entry(entry)
            cache.set_manifest(manifest_hash, manifest)

        return ensure_artifacts_exist(
            cache,
            cachekey,
            miss_reason,
            obj_file,
            compiler_result,
            add_manifest,
        )


def ensure_artifacts_exist(cache: Cache, cache_key: str,
                           reason: MissReason,
                           obj_file: Path,
                           compiler_result: Tuple[int, str, str],
                           action=None
                           ) -> Tuple[int, str, str]:
    '''
    Ensure that the artifacts for the given cache key exist.

    Parameters:
        cache (Cache): The cache to use.
        cache_key (str): The cache key to use.
        reason (Callable[[Statistics], None]): The reason for the cache miss.
        obj_file (Path): The object file to create.
        compiler_result (Tuple[int, str, str]): The result of the compiler invocation.
        action (Callable[[], None]): An optional action to perform if the artifacts are missing.

    Returns:
        Tuple[int, str, str]: A tuple containing the exit code, stdout and stderr.
    '''
    return_code, compiler_stdout, compiler_stderr = compiler_result
    compile_success = return_code == 0 and os.path.exists(obj_file)
    with cache.lock_for(cache_key):
        hit, _ = cache.has_entry(cache_key)
        if not hit:
            cache.statistics.record_cache_miss(reason)
            if compile_success:

                artifacts = CompilerArtifacts(
                    obj_file,
                    canonicalize_compile_output(
                        compiler_stdout, StdStream.STDOUT),
                    canonicalize_compile_output(
                        compiler_stderr, StdStream.STDERR),
                )

                trace(
                    f"Adding file {artifacts.obj_file_path} to cache using key {cache_key}")
                add_object_to_cache(cache, cache_key, artifacts)

            if action and compile_success:
                action()
    return return_code, compiler_stdout, compiler_stderr


def print_stdout_and_stderr(out: str, err: str):
    print_locked(sys.stdout, out)
    print_locked(sys.stderr, err)


def main() -> int:  # sourcery skip: de-morgan, extract-duplicate-method
    # Reconfigure the stdout / stderr streams to use the default codec used by the compiler
    sys.stdout.reconfigure(encoding=CL_DEFAULT_CODEC)  # type: ignore
    sys.stderr.reconfigure(encoding=CL_DEFAULT_CODEC)  # type: ignore

    # These Argparse Actions are necessary because the first commandline
    # argument, the compiler executable path, is optional, and the argparse
    # class does not support conditional selection of positional arguments.
    # Therefore, these classes check the candidate path, and if it is not an
    # executable, stores it in the namespace as a special variable, and
    # the compiler argument Action then prepends it to its list of arguments

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

    options: argparse.Namespace = parser.parse_args()

    with Cache() as cache:
        if options.show_stats:
            print_statistics(cache)
            return 0

        if options.clean_cache:
            clean_cache(cache)
            print("cache cleaned")
            return 0

        if options.clear_cache:
            clear_cache(cache)
            print("cache cleared")
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
    sys.exit(main())
