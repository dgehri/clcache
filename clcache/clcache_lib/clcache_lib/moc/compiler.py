
import contextlib
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from tempfile import TemporaryFile
from typing import BinaryIO, Dict, List, Optional, Tuple

from ..cache.cache import Cache, Location, ensure_artifacts_exist
from ..cache.ex import CompilerFailedException, IncludeNotFoundException
from ..cache.file_cache import Manifest, ManifestRepository, copy_from_cache
from ..cache.hash import get_compiler_hash, get_file_hash
from ..cache.manifest_entry import create_manifest_entry
from ..cache.stats import MissReason
from ..cache.virt import canonicalize_path, expand_path
from ..config.config import CL_DEFAULT_CODEC
from ..utils.args import (ArgumentQtLong, ArgumentQtLongWithParam,
                          ArgumentQtShort, ArgumentQtShortWithParam,
                          CommandLineAnalyzer, expand_response_file)
from ..utils.errors import *
from ..utils.file_lock import FileLock
from ..utils.logging import LogLevel, log
from ..utils.util import line_iter_b, print_stdout_and_stderr


class MocCommandLineAnalyzer(CommandLineAnalyzer):

    def __init__(self):

        args = {
            # /<NAME>[ =]parameter
            ArgumentQtShortWithParam("n"),
            ArgumentQtShortWithParam("o"),
            ArgumentQtShortWithParam("I"),
            ArgumentQtShortWithParam("F"),
            ArgumentQtShortWithParam("D"),
            ArgumentQtShortWithParam("U"),
            ArgumentQtShortWithParam("M"),
            ArgumentQtLongWithParam("compiler-flavor"),
            ArgumentQtShortWithParam("p"),
            ArgumentQtShortWithParam("f"),
            ArgumentQtShortWithParam("b"),
            ArgumentQtLongWithParam("include"),
            ArgumentQtShortWithParam("n"),
            ArgumentQtLongWithParam("dep-file-path"),
            ArgumentQtLongWithParam("dep-file-rule-name"),
            ArgumentQtLongWithParam("symbol-threshold"),
            ArgumentQtShort("h"),
            ArgumentQtShort("v"),
            ArgumentQtLong("version"),
            ArgumentQtShort("E"),
            ArgumentQtShort("i"),
            ArgumentQtLong("no-notes"),
            ArgumentQtLong("no-warnings"),
            ArgumentQtLong("ignore-option-clashes"),
            ArgumentQtLong("output-json"),
            ArgumentQtLong("collect-json"),
            ArgumentQtLong("output-dep-file"),
            ArgumentQtLong("has-symbol-threshold"),
            ArgumentQtLong("threshold-error"),
            ArgumentQtLong("show-include-hierarchy"),
            ArgumentQtLong("threshold-error-assert"),
            ArgumentQtLong("show-includes")
        }

        args_to_unify_and_sort = [
            ("I", True),
            ("p", True),
            ("f", True),
            ("b", True),
            ("b", True),
            ("o", True),
            ("include", False),
            ("v", False),
            ("version", False),
            ("E", False),
            ("i", False),
            ("no-notes", False),
            ("no-warnings", False),
            ("ignore-option-clashes", False),
            ("output-json", False),
            ("collect-json", False),
            ("output-dep-file", False),
            ("has-symbol-threshold", False),
            ("threshold-error", False),
            ("show-include-hierarchy", False),
            ("threshold-error-assert", False),
            ("show-includes", False),
            ("n", False),
            ("F", False),
            ("D", False),
            ("U", False),
            ("M", False),
            ("compiler-flavor", False),
            ("include", False),
            ("n", False),
            ("dep-file-rule-name", False),
            ("symbol-threshold", False)
        ]

        super().__init__(args=args,
                         args_to_unify_and_sort=args_to_unify_and_sort)

    def analyze(self, cmdline: List[str]) -> Tuple[Path, Optional[Path], Dict[str, List[str]]]:
        '''
        Analyzes the command line and returns a list of input and output files.

        Parameters:
            cmdline: The command line to analyze.

        Returns:
            Input header files and output moc file.            
        '''

        options, input_files = self.parse_args_and_input_files(
            cmdline)

        # Now collect the inputFiles into the return format
        if not input_files:
            raise NoSourceFileError()

        input_file = input_files[0]

        if "E" in options:
            raise CalledForPreprocessingError()

        if "output-json" in options or "collect-json" in options:
            raise CalledForJsonOutputError()

        output_file = None
        if "o" in options and options["o"][0]:
            output_file = Path(options["o"][0])
        else:
            raise CalledWithoutOutputFile()

        log(f"MOC input file: {input_file}")
        log(f"MOC output file: {output_file}")
        return input_file, output_file, options


def _invoke_real_compiler(compiler_path: Path,
                          cmd_line: List[str]) \
        -> Tuple[int, str, str]:
    '''Invoke the real compiler and return its exit code, stdout and stderr.'''

    read_cmd_line = [str(compiler_path)] + cmd_line

    # if command line longer than 32767 chars, use a response file
    # See https://devblogs.microsoft.com/oldnewthing/20031210-00/?p=41553
    if len(" ".join(read_cmd_line)) >= 32000:  # keep some chars as a safety margin
        with TemporaryFile(mode="wt", suffix=".rsp") as rsp_file:
            rsp_file.writelines(" ".join(cmd_line) + "\n")
            rsp_file.flush()
            return _invoke_real_compiler(
                compiler_path,
                [f"@{os.path.realpath(rsp_file.name)}"]
            )

    log(f"Invoking compiler: {read_cmd_line}")

    return_code: int = -1
    stdout: str = ""
    stderr: str = ""
    sys.stdout.flush()
    sys.stderr.flush()
    return_code = subprocess.call(read_cmd_line)

    log("Real compiler return code: {0:d}".format(return_code))

    return return_code, stdout, stderr


def _capture_real_compiler(compiler_path: Path,
                           cmd_line: List[str]) \
        -> Tuple[int, str, str]:
    '''Invoke the real compiler and return its exit code, stdout and stderr.'''

    read_cmd_line = [str(compiler_path)] + cmd_line

    # if command line longer than 32767 chars, use a response file
    # See https://devblogs.microsoft.com/oldnewthing/20031210-00/?p=41553
    if len(" ".join(read_cmd_line)) >= 32000:  # keep some chars as a safety margin
        with TemporaryFile(mode="wt", suffix=".rsp") as rsp_file:
            rsp_file.writelines(" ".join(cmd_line) + "\n")
            rsp_file.flush()
            return _capture_real_compiler(
                compiler_path,
                [f"@{os.path.realpath(rsp_file.name)}"]
            )

    log(f"Invoking compiler: {read_cmd_line}")

    return_code: int = -1
    stdout: str = ""
    stderr: str = ""

    # Don't use subprocess.communicate() here, it's slow due to internal
    # threading.
    with TemporaryFile() as stdout_file, TemporaryFile() as stderr_file:
        compilerProcess = subprocess.Popen(
            read_cmd_line, stdout=stdout_file, stderr=stderr_file
        )
        return_code = compilerProcess.wait()
        stdout_file.seek(0)
        stdout = stdout_file.read().decode(CL_DEFAULT_CODEC)
        stderr_file.seek(0)
        stderr = stderr_file.read().decode(CL_DEFAULT_CODEC)

    log("Real compiler return code: {0:d}".format(return_code))

    return return_code, stdout, stderr


def process_compile_request(cache: Cache, compiler: Path, args: List[str]) -> int:
    '''
    Process a compile request.

    Returns:
        The exit code of the compiler.
    '''
    log("Command line: '{0!s}'".format(" ".join(args)))

    cmdline = expand_response_file(args)

    log("Expanded commandline: '{0!s}'".format(" ".join(cmdline)))

    try:
        analyzer = MocCommandLineAnalyzer()
        header_file, output_file, options = analyzer.analyze(cmdline)
        return _schedule_jobs(
            cache, compiler, cmdline,
            header_file, output_file, analyzer, options
        )
    except InvalidArgumentError:
        log(f"Cannot cache invocation as {cmdline}: invalid argument",
            LogLevel.ERROR)
        cache.statistics.record_cache_miss(
            MissReason.CALL_WITH_INVALID_ARGUMENT)
    except NoSourceFileError:
        log(f"Cannot cache invocation as {cmdline}: no source file found")
        cache.statistics.record_cache_miss(MissReason.CALL_WITHOUT_SOURCE_FILE)
    except MultipleSourceFilesComplexError:
        log(
            f"Cannot cache invocation as {cmdline}: multiple source files found")
        cache.statistics.record_cache_miss(
            MissReason.CALL_WITH_MULTIPLE_SOURCE_FILES)
    except CalledWithPchError:
        log(
            f"Cannot cache invocation as {cmdline}: precompiled headers in use")
        cache.statistics.record_cache_miss(MissReason.CALL_WITH_PCH)
    except CalledForLinkError:
        log(f"Cannot cache invocation as {cmdline}: called for linking")
        cache.statistics.record_cache_miss(MissReason.CALL_FOR_LINKING)
    except ExternalDebugInfoError:
        log(
            f"Cannot cache invocation as {cmdline}: external debug information (/Zi) is not supported"
        )
        cache.statistics.record_cache_miss(
            MissReason.CALL_FOR_EXTERNAL_DEBUG_INFO)
    except CalledForPreprocessingError:
        log(
            f"Cannot cache invocation as {cmdline}: called for preprocessing")
        cache.statistics.record_cache_miss(MissReason.CALL_FOR_PREPROCESSING)

    except CalledForJsonOutputError:
        log(
            f"Cannot cache invocation as {cmdline}: called for JSON output")
        cache.statistics.record_cache_miss(MissReason.CACHE_FAILURE)

    except CalledWithoutOutputFile:
        log(
            f"Cannot cache invocation as {cmdline}: called without output file")
        cache.statistics.record_cache_miss(MissReason.CACHE_FAILURE)

    exit_code, _, _ = _invoke_real_compiler(compiler, args)
    return exit_code


def _schedule_jobs(
    cache: Cache,
    compiler: Path,
    cmd_line: List[str],
    header_file: Path,
    output_file: Optional[Path],
    analyzer: MocCommandLineAnalyzer,
    options: Dict[str, List[str]]
) -> int:
    '''
    Schedule jobs for the given command line.

    Parameters:
    '''
    exit_code: int = 0
    exit_code, out, err = _process_single_source(
        cache, compiler, cmd_line, header_file,
        output_file, analyzer, options
    )
    log("Finished. Exit code {0:d}".format(exit_code), force_flush=True)
    print_stdout_and_stderr(out, err, CL_DEFAULT_CODEC)

    return exit_code


def _process_single_source(cache: Cache,
                           compiler: Path,
                           cmdline: List[str],
                           header_file: Path,
                           output_file: Optional[Path],
                           analyzer: MocCommandLineAnalyzer,
                           options: Dict[str, List[str]]) \
        -> Tuple[int, str, str]:
    '''
    Process a single source file.
        Returns:
            Tuple[int, str, str]: A tuple containing the exit code, stdout, stderr
    '''
    try:
        assert output_file is not None
        return _process(cache, output_file, compiler, cmdline, header_file, analyzer, options)

    except CompilerFailedException as e:
        return e.get_compiler_result()
    except Exception as e:
        log(f"Exception occurred: {traceback.format_exc()}", LogLevel.ERROR)
        return _invoke_real_compiler(compiler, cmdline)


def _process(cache: Cache,
             output_file: Path,
             compiler_path: Path,
             cmdline: List[str],
             header_file: Path,
             analyzer: MocCommandLineAnalyzer,
             options: Dict[str, List[str]]) -> Tuple[int, str, str]:
    '''
    Process a single source file.

        Parameters:
            cache (Cache): The cache to use.
            obj_file (Path): The object file to create.
            compiler (Path): The path to the compiler.
            src_file (Path): The source file to compile.

        Returns:
            Tuple[int, str, str]: A tuple containing the exit code, stdout, stderr
    '''

    # Get manifest hash
    manifest_hash: str = _get_manifest_hash(
        compiler_path, cmdline, header_file, analyzer)

    # Acquire lock for manifest hash to prevent two jobs from compiling the same source
    # file at the same time. This is a frequent situation on Jenkins, and having the 2nd
    # job wait for the 1st job to finish compiling the source file is more efficient overall.
    with FileLock(manifest_hash, 120*1000*1000):
        manifest_hit = False
        cache_key = None

        with cache.manifest_lock_for(manifest_hash):
            try:
                # Get the manifest for the manifest hash (if it exists)
                if manifest_info := cache.get_manifest(manifest_hash):
                    manifest, _ = manifest_info

                    # Check if manifest entry exists
                    for entry_index, entry in enumerate(manifest.entries()):

                        # NOTE: command line options already included in hash for manifest name
                        with contextlib.suppress(IncludeNotFoundException):

                            # Get hash of include files
                            includes_content_hash = (
                                ManifestRepository.get_includes_content_hash_for_files(
                                    [expand_path(path)
                                     for path in entry.includeFiles]
                                )
                            )

                            # Check if include files have changed, if so, skip this entry
                            if entry.includesContentHash != includes_content_hash:
                                continue

                            # Include files have not changed, we have a hit!
                            cache_key = entry.objectHash
                            manifest_hit = True

                            # Move manifest entry to the top of the entries in the manifest
                            # (if not already at top), so that we can use LRU replacement
                            if entry_index > 0:
                                manifest.touch_entry(cache_key)
                                cache.set_manifest(manifest_hash, manifest)

                            # Check if object file exists in cache
                            with cache.lock_for(cache_key):
                                hit = cache.has_entry(cache_key)
                                if hit:
                                    # Object cache hit!
                                    result = _process_cache_hit(
                                        cache, output_file, cache_key)

                                    if "output-dep-file" in options:
                                        # Determine depencency file path
                                        dep_file_path = output_file.parent / \
                                            f"{output_file.name}.d"
                                        _safe_unlink(dep_file_path)

                                        # Determine target name
                                        rule_name = output_file
                                        if "dep-file-rule-name" in options:
                                            rule_name = Path(
                                                options["dep-file-rule-name"][0])

                                        # create depencency file
                                        _create_dep_file(
                                            dep_file_path, rule_name, entry.includeFiles)

                                    return result

                    miss_reason = MissReason.HEADER_CHANGED_MISS
                else:
                    miss_reason = MissReason.SOURCE_CHANGED_MISS
            except Exception:
                cache.statistics.record_cache_miss(MissReason.CACHE_FAILURE)
                raise

        # If we get here, we have a cache miss and we'll need to invoke the real compiler
        if manifest_hit:
            # Got a manifest, but no object => invoke real compiler
            compiler_result = _capture_real_compiler(
                compiler_path, cmdline)

            with cache.manifest_lock_for(manifest_hash):
                assert cache_key is not None
                return ensure_artifacts_exist(
                    cache,
                    cache_key,
                    miss_reason,
                    output_file,
                    compiler_result,
                    output_file_filter=lambda i, o: _canonicalize_moc_output_file(
                        i, o, output_file)
                )
        else:
            # Also generate manifest
            remove_dep_file = False
            if "--output-dep-file" not in cmdline:
                cmdline = list(cmdline)
                cmdline.insert(0, "--output-dep-file")
                remove_dep_file = True

            # Invoke real compiler and get output
            compiler_result = _capture_real_compiler(
                compiler_path, cmdline)

            # Parse dependency file
            dep_file_path = output_file.parent / f"{output_file.name}.d"
            include_paths = _parse_dep_file(dep_file_path)
            if remove_dep_file:
                dep_file_path.unlink()

            # Create manifest entry
            entry = create_manifest_entry(manifest_hash, include_paths)
            cache_key = entry.objectHash

            def add_manifest() -> int:
                with cache.manifest_lock_for(manifest_hash):
                    if manifest_info := cache.get_manifest(manifest_hash):
                        manifest, old_size = manifest_info
                    else:
                        manifest = Manifest()
                        old_size = 0

                    manifest.add_entry(entry)
                    new_size = cache.set_manifest(
                        manifest_hash, manifest, location=Location.LOCAL)

                # Setting remote manifest outside lock
                cache.set_manifest(manifest_hash, manifest, Location.REMOTE)
                return new_size - old_size

            return ensure_artifacts_exist(
                cache,
                cache_key,
                miss_reason,
                output_file,
                compiler_result,
                post_commit_action=add_manifest,
                output_file_filter=lambda i, o: _canonicalize_moc_output_file(
                    i, o, output_file)
            )


def _canonicalize_moc_output_file(file_in: BinaryIO,
                                  file_out: BinaryIO,
                                  output_file: Path):
    '''
    Canonicalizes the include statements in the MOC file (output_file)
    '''
    re_include = re.compile(rb"^#include\s+\"(.*)\"")
    filter_line = True
    for line in line_iter_b(file_in.read()):
        if filter_line:
            if line == "QT_BEGIN_MOC_NAMESPACE".encode():
                filter_line = False
            elif m := re_include.match(line):
                include_path = Path(m[1].decode())
                if not include_path.is_absolute():
                    include_path = output_file.parent / include_path

                collapsed_path = canonicalize_path(
                    include_path.resolve().absolute())
                line = (
                    line[: m.start(1)]
                    + collapsed_path.encode()
                ) + line[m.end(1):]

        # write to output file
        file_out.write(line + b"\r\n")


def _get_manifest_hash(compiler_path: Path,
                       cmd_line: List[str],
                       src_file: Path,
                       analyzer: MocCommandLineAnalyzer) -> str:
    '''
    Returns a hash of the manifest file that would be used for the given command line.
    '''
    compiler_hash = get_compiler_hash(compiler_path)

    (
        args,
        input_files,
    ) = MocCommandLineAnalyzer().parse_args_and_input_files(cmd_line)

    assert input_files
    input_file = input_files[0]

    def canonicalize_path_arg(arg: Path):
        return canonicalize_path(arg.absolute())

    cmd_line = []

    args_to_unify_and_sort = analyzer.get_args_to_unify_and_sort()

    # We only sort the arguments, not their values because the
    # order of the latter may change the compiler result.
    for k in sorted(args.keys()):
        if k in args_to_unify_and_sort:
            if args_to_unify_and_sort[k]:
                cmd_line.extend(
                    [f"/{k}{canonicalize_path_arg(Path(value))}" for value in args[k]]
                )
            else:
                cmd_line.extend(
                    [f"/{k}{value}" for value in list(dict.fromkeys(args[k]))]
                )
        else:
            cmd_line.extend([f"/{k}{value}" for value in args[k]])

    cmd_line.extend([canonicalize_path_arg(input_file)])

    toolset_data = "{}|{}|{}".format(
        compiler_hash, cmd_line, ManifestRepository.MANIFEST_FILE_MOC_FORMAT_VERSION
    )
    return get_file_hash(src_file, toolset_data)


def _safe_unlink(path: Path) -> None:
    if not path.exists():
        return
    success = False
    for _ in range(60):
        try:
            path.unlink()
            success = True
            break
        except Exception:
            time.sleep(1)

    if not success:
        path.unlink()


def _create_dep_file(dep_file_path: Path,
                     rule: Path,
                     include_paths: List[str]) -> None:
    """Create a dependency file."""
    def escaped_path(path: Path) -> str:
        return path.as_posix().replace("\\", "\\\\").replace(" ", "\\ ")

    with dep_file_path.open("w") as dep_file:
        dep_file.write(f"{escaped_path(rule)}:")

        for include_path in include_paths:
            # expand include_path
            expanded_include_path = expand_path(include_path)

            dep_file.write(f" \\\n  {escaped_path(expanded_include_path)}")

        dep_file.write("\n")


def _process_cache_hit(cache: Cache,
                       output_file: Path,
                       cache_key: str) -> Tuple[int, str, str]:
    """
    Process a cache hit, copying the object file from the cache to the output directory.

        Parameters:
            cache: The cache to use.
            obj_file: The object file to write.
            cache_key: The cache key to use.
            include_files: The list of include files to write to the depencency file.

        Returns:
            A tuple of the exit code, the stdout, the stderr
    """
    log(
        f"Reusing cached object for key {cache_key} for output file {output_file}")

    with cache.lock_for(cache_key):
        cache.statistics.record_cache_hit()

        _safe_unlink(output_file)

        cached_artifacts = cache.get_entry(cache_key)
        assert cached_artifacts is not None

        def copy_filter(file_in, file_out):
            '''
            Expands the include statements in the MOC file
            '''
            re_include = re.compile(rb"^#include\s+\"(.*)\"")
            filter_line = True
            for line in line_iter_b(file_in.read()):
                if filter_line:
                    if line == "QT_BEGIN_MOC_NAMESPACE".encode():
                        filter_line = False

                    elif m := re_include.match(line):
                        include_path = m[1].decode()
                        expanded_path = expand_path(include_path).resolve()
                        # calculate relative path from output file to include file
                        with contextlib.suppress(ValueError):
                            expanded_path = Path(os.path.relpath(expanded_path, output_file.parent))
                        line = (
                            line[: m.start(1)]
                            + expanded_path.as_posix().encode()
                        ) + line[m.end(1):]

                # write to output file
                file_out.write(line + b"\r\n")

        copy_from_cache(cached_artifacts.payload_path,
                        output_file, copy_filter)
        return (
            0,
            cached_artifacts.stdout,
            cached_artifacts.stderr
        )


def _parse_dep_file(dep_file_path: Path) -> List[Path]:
    """Parse a dependency file and return the list of included files."""
    # extract depencencies from a makefile-style dep file
    with open(dep_file_path, "r") as dep_file:
        buf = dep_file.read()

        # find the first colon, which separates the target from the dependencies,
        # but ignore drive letters followed by a colon, which are part of Windows paths.
        if m := re.match(r"^(\s*(?:[a-zA-Z]:)?[^:]*:)", buf):
            buf = buf[m.end() + 1:]

            # join lines ending with a backslash
            buf = re.sub(r"\\\r?\n", "", buf)

            # split at whitespace not preceded by a backslash
            lines = filter(None, re.split(r"(?<!\\)\s+", buf))

            # convert the list of strings into a list of Path objects
            return [Path(os.path.normpath(line.replace("\\", "").strip())).absolute() for line in lines]

    return []
