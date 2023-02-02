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
from typing import Any, Iterator, List, Tuple

from clcache_lib.utils import *
from clcache_lib.cache import *
from clcache_lib.cl import *
from clcache_lib.config import VERSION

# Returns pair:
#   1. set of include filepaths
#   2. new compiler output
# Output changes if strip is True in that case all lines with include
# directives are stripped from it
def parse_includes_set(compilerOutput, sourceFile, strip):
    newOutput = []
    includesSet = set()

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
    reFilePath = re.compile(r"^(\w+): ([ \w]+):( +)(?P<file_path>\S.*)$")

    absSourceFile = os.path.normcase(os.path.abspath(sourceFile))
    for line in compilerOutput.splitlines(True):
        match = reFilePath.match(line.rstrip("\r\n"))
        if match is not None:
            filePath = match["file_path"]
            filePath = os.path.normcase(os.path.abspath(os.path.normpath(filePath)))
            if filePath != absSourceFile:
                includesSet.add(filePath)
        elif strip:
            newOutput.append(line)
    if strip:
        return includesSet, "".join(newOutput)
    else:
        return includesSet, compilerOutput


def process_cache_hit(cache, is_local, obj_file, cache_key):
    trace(f"Reusing cached object for key {cache_key} for object file {obj_file}")

    with cache.lockFor(cache_key):
        with cache.statistics.lock, cache.statistics as stats:
            stats.registerCacheHit(is_local)

        if os.path.exists(obj_file):
            success = False
            for _ in range(60):
                try:
                    os.remove(obj_file)
                    success = True
                    break
                except Exception:
                    time.sleep(1)

            if not success:
                os.remove(obj_file)

        cachedArtifacts = cache.getEntry(cache_key)
        copy_or_link(cachedArtifacts.objectFilePath, obj_file)
        trace("Finished. Exit code 0")
        return (
            0,
            expandDirPlaceholderInCompileOutput(cachedArtifacts.stdout, RE_STDOUT),
            expandDirPlaceholderInCompileOutput(cachedArtifacts.stderr, RE_STDERR),
            False,
        )


def create_manifest_entry(manifestHash, includePaths):
    sortedIncludePaths = sorted(set(includePaths))
    includeHashes = getFileHashes(sortedIncludePaths)

    safeIncludes = [collapseDirToPlaceholder(path) for path in sortedIncludePaths]
    includesContentHash = ManifestRepository.getIncludesContentHashForHashes(
        includeHashes
    )
    cachekey = CompilerArtifactsRepository.compute_key(
        manifestHash, includesContentHash
    )

    return ManifestEntry(safeIncludes, includesContentHash, cachekey)


def process_compile_request(cache, compiler, args):
    trace("Parsing given commandline '{0!s}'".format(args))

    cmdLine, environment = extend_cmdline_from_env(args, os.environ)
    cmdLine = expand_cmdline(cmdLine)
    trace("Expanded commandline '{0!s}'".format(cmdLine))

    try:
        sourceFiles, objectFiles = CommandLineAnalyzer.analyze(cmdLine)
        return schedule_jobs(
            cache, compiler, cmdLine, environment, sourceFiles, objectFiles
        )
    except InvalidArgumentError:
        trace(f"Cannot cache invocation as {cmdLine}: invalid argument")
        updateCacheStatistics(cache, Statistics.registerCallWithInvalidArgument)
    except NoSourceFileError:
        trace(f"Cannot cache invocation as {cmdLine}: no source file found")
        updateCacheStatistics(cache, Statistics.registerCallWithoutSourceFile)
    except MultipleSourceFilesComplexError:
        trace(f"Cannot cache invocation as {cmdLine}: multiple source files found")
        updateCacheStatistics(cache, Statistics.registerCallWithMultipleSourceFiles)
    except CalledWithPchError:
        trace(f"Cannot cache invocation as {cmdLine}: precompiled headers in use")
        updateCacheStatistics(cache, Statistics.registerCallWithPch)
    except CalledForLinkError:
        trace(f"Cannot cache invocation as {cmdLine}: called for linking")
        updateCacheStatistics(cache, Statistics.registerCallForLinking)
    except ExternalDebugInfoError:
        trace(
            f"Cannot cache invocation as {cmdLine}: external debug information (/Zi) is not supported"
        )
        updateCacheStatistics(cache, Statistics.registerCallForExternalDebugInfo)
    except CalledForPreprocessingError:
        trace(f"Cannot cache invocation as {cmdLine}: called for preprocessing")
        updateCacheStatistics(cache, Statistics.registerCallForPreprocessing)

    exitCode, out, err = invoke_real_compiler(compiler, args)
    print_stdout_and_stderr(out, err)
    return exitCode


def filter_source_files(
    cmdLine: List[str], sourceFiles: List[Tuple[str, str]]
) -> Iterator[str]:
    setOfSources = {sourceFile for sourceFile, _ in sourceFiles}
    skippedArgs = ("/Tc", "/Tp", "-Tp", "-Tc")
    yield from (
        arg
        for arg in cmdLine
        if not (arg in setOfSources or arg.startswith(skippedArgs))
    )


def schedule_jobs(
    cache: Any,
    compiler: str,
    cmdLine: List[str],
    environment: Any,
    sourceFiles: List[Tuple[str, str]],
    objectFiles: List[str],
) -> int:
    # Filter out all source files from the command line to form baseCmdLine
    baseCmdLine = [
        arg
        for arg in filter_source_files(cmdLine, sourceFiles)
        if not arg.startswith("/MP")
    ]

    exitCode = 0
    cleanupRequired = False

    if (len(sourceFiles) == 1 and len(objectFiles) == 1) or os.getenv(
        "CLCACHE_SINGLEFILE"
    ):
        assert len(sourceFiles) == 1
        assert len(objectFiles) == 1
        srcFile, srcLanguage = sourceFiles[0]
        objFile = objectFiles[0]
        jobCmdLine = baseCmdLine + [srcLanguage + srcFile]
        exitCode, out, err, doCleanup = process_single_source(
            cache, compiler, jobCmdLine, srcFile, objFile, environment
        )
        trace("Finished. Exit code {0:d}".format(exitCode))
        cleanupRequired |= doCleanup
        print_stdout_and_stderr(out, err)
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=job_count(cmdLine)
        ) as executor:
            jobs = []
            for (srcFile, srcLanguage), objFile in zip(sourceFiles, objectFiles):
                jobCmdLine = baseCmdLine + [srcLanguage + srcFile]
                jobs.append(
                    executor.submit(
                        process_single_source,
                        cache,
                        compiler,
                        jobCmdLine,
                        srcFile,
                        objFile,
                        environment,
                    )
                )
            for future in concurrent.futures.as_completed(jobs):
                exitCode, out, err, doCleanup = future.result()
                trace("Finished. Exit code {0:d}".format(exitCode))
                cleanupRequired |= doCleanup
                print_stdout_and_stderr(out, err)

                if exitCode != 0:
                    break

    if cleanupRequired:
        try:
            cleanCache(cache)
        except CacheLockException as e:
            trace(repr(e))

    return exitCode


def process_single_source(cache, compiler, cmdLine, sourceFile, objectFile, environment):
    try:
        assert objectFile is not None
        return process(cache, objectFile, compiler, cmdLine, sourceFile)

    except IncludeNotFoundException:
        return *invoke_real_compiler(compiler, cmdLine, environment=environment), False
    except OSError:
        return *invoke_real_compiler(compiler, cmdLine, environment=environment), False
    except CompilerFailedException as e:
        return e.getReturnTuple()
    except CacheLockException as e:
        trace(repr(e))
        return *invoke_real_compiler(compiler, cmdLine, environment=environment), False


def process(cache, objectFile, compiler, cmdLine, sourceFile):
    manifestHash = ManifestRepository.getManifestHash(compiler, cmdLine, sourceFile)
    manifestHit = None
    with cache.manifestLockFor(manifestHash):
        manifest = cache.getManifest(manifestHash)
        if manifest:
            for entryIndex, entry in enumerate(manifest.entries()):
                # NOTE: command line options already included in hash for manifest name
                with contextlib.suppress(IncludeNotFoundException):
                    includesContentHash = (
                        ManifestRepository.getIncludesContentHashForFiles(
                            [expandDirPlaceholder(path) for path in entry.includeFiles]
                        )
                    )

                    if entry.includesContentHash == includesContentHash:
                        cachekey = entry.objectHash
                        assert cachekey is not None
                        if entryIndex > 0:
                            # Move manifest entry to the top of the entries in the manifest
                            manifest.touchEntry(cachekey)
                            cache.setManifest(manifestHash, manifest)

                        manifestHit = True
                        with cache.lockFor(cachekey):
                            hit, is_local = cache.hasEntry(cachekey)
                            if hit:
                                return process_cache_hit(cache, is_local, objectFile, cachekey)

            unusableManifestMissReason = Statistics.registerHeaderChangedMiss
        else:
            unusableManifestMissReason = Statistics.registerSourceChangedMiss

    if manifestHit is None:
        stripIncludes = False
        if "/showIncludes" not in cmdLine:
            cmdLine = list(cmdLine)
            cmdLine.insert(0, "/showIncludes")
            stripIncludes = True

    compilerResult = invoke_real_compiler(compiler, cmdLine, captureOutput=True)

    if manifestHit is None:
        includePaths, compilerOutput = parse_includes_set(
            compilerResult[1], sourceFile, stripIncludes
        )
        compilerResult = (compilerResult[0], compilerOutput, compilerResult[2])

    with cache.manifestLockFor(manifestHash):
        if manifestHit is not None:
            return ensure_artifacts_exist(
                cache, cachekey, unusableManifestMissReason, objectFile, compilerResult
            )

        entry = create_manifest_entry(manifestHash, includePaths)
        cachekey = entry.objectHash

        def addManifest():
            manifest = cache.getManifest(manifestHash) or Manifest()
            manifest.addEntry(entry)
            cache.setManifest(manifestHash, manifest)

        return ensure_artifacts_exist(
            cache,
            cachekey,
            unusableManifestMissReason,
            objectFile,
            compilerResult,
            addManifest,
        )

def ensure_artifacts_exist(
    cache, cachekey, reason, objectFile, compilerResult, extraCallable=None
):
    cleanupRequired = False
    returnCode, compilerOutput, compilerStderr = compilerResult
    correctCompiliation = returnCode == 0 and os.path.exists(objectFile)
    with cache.lockFor(cachekey):
        hit, _ = cache.hasEntry(cachekey)
        if not hit:
            with cache.statistics.lock, cache.statistics as stats:
                reason(stats)
                if correctCompiliation:
                    artifacts = CompilerArtifacts(
                        objectFile,
                        collapseDirPlaceholderInCompileOutput(
                            compilerOutput, RE_STDOUT
                        ),
                        collapseDirPlaceholderInCompileOutput(
                            compilerStderr, RE_STDERR
                        ),
                    )

                    trace(f"Adding file {artifacts.objectFilePath} to cache using key {cachekey}")
                    cleanupRequired = addObjectToCache(
                        stats, cache, cachekey, artifacts
                    )
            if extraCallable and correctCompiliation:
                extraCallable()
    return returnCode, compilerOutput, compilerStderr, cleanupRequired


def print_stdout_and_stderr(out, err):
    print_binary(sys.stdout, out.encode(CL_DEFAULT_CODEC))
    print_binary(sys.stderr, err.encode(CL_DEFAULT_CODEC))
    

def main():
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
    groupParser = parser.add_mutually_exclusive_group()
    groupParser.add_argument(
        "-s",
        "--stats",
        dest="show_stats",
        action="store_true",
        help="print cache statistics",
    )
    groupParser.add_argument(
        "-c", "--clean", dest="clean_cache", action="store_true", help="clean cache"
    )
    groupParser.add_argument(
        "-C", "--clear", dest="clear_cache", action="store_true", help="clear cache"
    )
    groupParser.add_argument(
        "-z",
        "--reset",
        dest="reset_stats",
        action="store_true",
        help="reset cache statistics",
    )
    groupParser.add_argument(
        "-M",
        "--set-size",
        dest="cache_size",
        type=int,
        default=None,
        help="set maximum cache size (in bytes)",
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

    options = parser.parse_args()

    cache = Cache()

    if options.show_stats:
        printStatistics(cache)
        return 0

    if options.clean_cache:
        cleanCache(cache)
        print("cache cleaned")
        return 0

    if options.clear_cache:
        clearCache(cache)
        print("cache cleared")
        return 0

    if options.reset_stats:
        resetStatistics(cache)
        print("Statistics reset")
        return 0

    if options.cache_size is not None:
        maxSizeValue = options.cache_size
        if maxSizeValue < 1:
            print("Max size argument must be greater than 0.", file=sys.stderr)
            return 1

        with cache.lock, cache.configuration as cfg:
            cfg.setMaximumCacheSize(maxSizeValue)
        return 0

    compiler = options.compiler or find_compiler_binary()
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


def main_wrapper():
    trace(f"BASEDIR = {BASEDIR}")
    trace(f"BUILDDIR = {BUILDDIR}")

    if "CLCACHE_PROFILE" in os.environ:
        INVOCATION_HASH = getStringHash(",".join(sys.argv))
        CALL_SCRIPT = """
import clcache
returnCode = clcache.__main__.main()
if returnCode != 0:
    raise clcache.__main__.ProfilerError(returnCode)
"""
        try:
            cProfile.run(CALL_SCRIPT, filename=f"clcache-{INVOCATION_HASH}.prof")
        except ProfilerError as e:
            sys.exit(e.returnCode)
    else:
        sys.exit(main())


if __name__ == "__main__":
    main_wrapper()
