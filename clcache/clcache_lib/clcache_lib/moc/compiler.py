
import contextlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from shutil import which
from tempfile import TemporaryFile
from typing import Callable, Dict, List, Optional, Tuple

from ..cache.cache import Cache, add_object_to_cache
from ..cache.cache_lock import CacheLock
from ..cache.ex import CompilerFailedException, IncludeNotFoundException
from ..cache.file_cache import CompilerArtifacts, Manifest, ManifestRepository
from ..cache.hash import get_compiler_hash, get_file_hash
from ..cache.manifest_entry import create_manifest_entry
from ..cache.stats import MissReason
from ..cache.virt import (StdStream, canonicalize_compile_output,
                          canonicalize_path, expand_compile_output,
                          expand_path)
from ..config.config import CL_DEFAULT_CODEC
from ..utils.args import (ArgumentQtLong, ArgumentQtLongWithParam,
                          ArgumentQtShort, ArgumentQtShortWithParam,
                          CommandLineAnalyzer, expand_response_file)
from ..utils.errors import *
from ..utils.util import copy_from_cache, print_stdout_and_stderr, trace


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

    def analyze(self, cmdline: List[str]) -> Tuple[Path, Optional[Path]]:
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

        output_file = None
        if "o" in options and options["o"][0]:
            output_file = Path(options["o"][0])

        trace(f"MOC input file: {input_file}")
        trace(f"MOC output file: {output_file}")
        return input_file, output_file


def find_compiler_binary() -> Optional[Path]:
    """Find the moc compiler binary."""
    if "MOCCACHE_MOC" in os.environ:
        path: Path = Path(os.environ["MOCCACHE_MOC"])

        # If the path is not absolute, try to find it in the PATH
        if path.name == path:
            if p := which(path):
                path = Path(p)

        return path if path is not None and path.exists() else None

    return Path(p) if (p := which("moc.exe")) else None


def invoke_real_compiler(compiler_path: Path,
                         cmd_line: List[str],
                         capture_output: bool = False,
                         environment: Optional[Dict[str, str]] = None) \
        -> Tuple[int, str, str]:
    '''Invoke the real compiler and return its exit code, stdout and stderr.'''

    read_cmd_line = [str(compiler_path)] + cmd_line

    # if command line longer than 32767 chars, use a response file
    # See https://devblogs.microsoft.com/oldnewthing/20031210-00/?p=41553
    if len(" ".join(read_cmd_line)) >= 32000:  # keep some chars as a safety margin
        with TemporaryFile(mode="wt", suffix=".rsp") as rsp_file:
            rsp_file.writelines(" ".join(cmd_line) + "\n")
            rsp_file.flush()
            return invoke_real_compiler(
                compiler_path,
                [f"@{os.path.realpath(rsp_file.name)}"],
                capture_output,
                environment,
            )

    trace(f"Invoking real compiler as {read_cmd_line}")

    environment = environment or dict(os.environ)

    # Environment variable set by the Visual Studio IDE to make cl.exe write
    # Unicode output to named pipes instead of stdout. Unset it to make sure
    # we can catch stdout output.
    environment.pop("VS_UNICODE_OUTPUT", None)

    return_code: int = -1
    stdout: str = ""
    stderr: str = ""
    if capture_output:
        # Don't use subprocess.communicate() here, it's slow due to internal
        # threading.
        with TemporaryFile() as stdout_file, TemporaryFile() as stderr_file:
            compilerProcess = subprocess.Popen(
                read_cmd_line, stdout=stdout_file, stderr=stderr_file, env=environment
            )
            return_code = compilerProcess.wait()
            stdout_file.seek(0)
            stdout = stdout_file.read().decode(CL_DEFAULT_CODEC)
            stderr_file.seek(0)
            stderr = stderr_file.read().decode(CL_DEFAULT_CODEC)
    else:
        sys.stdout.flush()
        sys.stderr.flush()
        return_code = subprocess.call(read_cmd_line, env=environment)

    trace("Real compiler returned code {0:d}".format(return_code))

    return return_code, stdout, stderr


def process_compile_request(cache: Cache, compiler: Path, args: List[str]) -> int:
    '''
    Process a compile request.

    Returns:
        The exit code of the compiler.
    '''
    trace("Parsing given commandline '{0!s}'".format(args))

    cmdline = expand_response_file(args)
    environment = dict(os.environ)

    trace("Expanded commandline '{0!s}'".format(cmdline))

    try:
        analyzer = MocCommandLineAnalyzer()
        header_file, output_file = analyzer.analyze(cmdline)
        return _schedule_jobs(
            cache, compiler, cmdline, environment, header_file, output_file, analyzer
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
    print_stdout_and_stderr(out, err, CL_DEFAULT_CODEC)
    return exit_code


def _schedule_jobs(
    cache: Cache,
    compiler: Path,
    cmd_line: List[str],
    environment: Dict[str, str],
    header_file: Path,
    output_file: Optional[Path],
    analyzer: MocCommandLineAnalyzer
) -> int:
    '''
    Schedule jobs for the given command line.

    Parameters:
    '''
    exit_code: int = 0
    exit_code, out, err = _process_single_source(
        cache, compiler, cmd_line, header_file, output_file, environment, analyzer
    )
    trace("Finished. Exit code {0:d}".format(exit_code))
    print_stdout_and_stderr(out, err, CL_DEFAULT_CODEC)

    return exit_code


def _process_single_source(cache: Cache,
                           compiler: Path,
                           cmdline: List[str],
                           header_file: Path,
                           output_file: Optional[Path],
                           environment: Optional[Dict[str, str]],
                           analyzer: MocCommandLineAnalyzer) \
        -> Tuple[int, str, str]:
    '''
    Process a single source file.
        Returns:
            Tuple[int, str, str]: A tuple containing the exit code, stdout, stderr
    '''
    try:
        assert output_file is not None
        return _process(cache, output_file, compiler, cmdline, header_file, analyzer)

    except CompilerFailedException as e:
        return e.get_compiler_result()
    except Exception as e:
        trace(f"Exception occurred: {e}")
        return invoke_real_compiler(compiler, cmdline, environment=environment)


def _process(cache: Cache,
             output_file: Path,
             compiler_path: Path,
             cmdline: List[str],
             header_file: Path,
             analyzer: MocCommandLineAnalyzer) -> Tuple[int, str, str]:
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
    with CacheLock(manifest_hash, 120*1000*1000):
        manifest_hit = False
        cachekey = None

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
                            cachekey = entry.objectHash
                            manifest_hit = True

                            # Move manifest entry to the top of the entries in the manifest
                            # (if not already at top), so that we can use LRU replacement
                            if entry_index > 0:
                                manifest.touch_entry(cachekey)
                                cache.set_manifest(manifest_hash, manifest)

                            # Check if object file exists in cache
                            with cache.lock_for(cachekey):
                                hit, _ = cache.has_entry(cachekey)
                                if hit:
                                    # Object cache hit!
                                    return _process_cache_hit(cache, output_file, cachekey, entry.includeFiles)

                    miss_reason = MissReason.HEADER_CHANGED_MISS
                else:
                    miss_reason = MissReason.SOURCE_CHANGED_MISS
            except Exception:
                cache.statistics.record_cache_miss(MissReason.CACHE_FAILURE)
                raise

        # If we get here, we have a cache miss and we'll need to invoke the real compiler
        if manifest_hit:
            # Got a manifest, but no object => invoke real compiler
            compiler_result = invoke_real_compiler(
                compiler_path, cmdline, capture_output=True)

            with cache.manifest_lock_for(manifest_hash):
                assert cachekey is not None
                return ensure_artifacts_exist(
                    cache, cachekey, miss_reason, output_file, compiler_result
                )
        else:
            # Also generate manifest
            remove_dep_file = False
            if "--output-dep-file" not in cmdline:
                # Ensure compiler dumps include files, but strip them
                # before printing to stdout, unless --show-includes is used
                cmdline = list(cmdline)
                cmdline.insert(0, "--output-dep-file")
                remove_dep_file = True

            # Invoke real compiler and get output
            compiler_result = invoke_real_compiler(
                compiler_path, cmdline, capture_output=True)

            # Parse dependency file
            dep_file_path = output_file.parent / f"{output_file.name}.d"
            include_paths = _parse_dep_file(dep_file_path)
            if remove_dep_file:
                dep_file_path.unlink()

            # Create manifest entry
            entry = create_manifest_entry(manifest_hash, include_paths)
            cachekey = entry.objectHash

            def add_manifest() -> int:
                with cache.manifest_lock_for(manifest_hash):
                    if manifest_info := cache.get_manifest(manifest_hash):
                        manifest, old_size = manifest_info
                    else:
                        manifest = Manifest()
                        old_size = 0

                    manifest.add_entry(entry)
                    new_size = cache.set_manifest(manifest_hash, manifest)
                    return new_size - old_size

            return ensure_artifacts_exist(
                cache,
                cachekey,
                miss_reason,
                output_file,
                compiler_result,
                add_manifest,
            )


def ensure_artifacts_exist(cache: Cache, cache_key: str,
                           reason: MissReason,
                           obj_file: Path,
                           compiler_result: Tuple[int, str, str],
                           action: Optional[Callable[[], int]] = None
                           ) -> Tuple[int, str, str]:
    '''
    Ensure that the artifacts for the given cache key exist.

    Parameters:
        cache (Cache): The cache to use.
        cache_key (str): The cache key to use.
        reason (Callable[[Statistics], None]): The reason for the cache miss.
        obj_file (Path): The object file to create.
        compiler_result (Tuple[int, str, str]): The result of the compiler invocation.
        action (Callable[[], None]): An optional action to perform unconditionally.

    Returns:
        Tuple[int, str, str]: A tuple containing the exit code, stdout and stderr.
    '''
    return_code, compiler_stdout, compiler_stderr = compiler_result
    if return_code == 0 and obj_file.exists():
        artifacts = CompilerArtifacts(
            obj_file,
            canonicalize_compile_output(
                compiler_stdout, StdStream.STDOUT),
            canonicalize_compile_output(
                compiler_stderr, StdStream.STDERR),
        )

        trace(
            f"Adding file {artifacts.obj_file_path} to cache using key {cache_key}")

        add_object_to_cache(cache, cache_key, artifacts, reason, action)

    return return_code, compiler_stdout, compiler_stderr


def _get_manifest_hash(compiler_path: Path, cmd_line: List[str], src_file: Path, analyzer: MocCommandLineAnalyzer) -> str:
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
        compiler_hash, cmd_line, ManifestRepository.MANIFEST_FILE_FORMAT_VERSION
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
                     obj_file: Path,
                     include_paths: List[Path]) -> None:
    with dep_file_path.open("w") as dep_file:
        dep_file.write(f"{obj_file.as_posix()}: ")

        for include_path in include_paths:
            # expand include_path
            expanded_include_path = expand_path(include_path)
            
            dep_file.write(f" \\\n  {expanded_include_path.as_posix()}")
            
        dep_file.write("\n")


def _process_cache_hit(cache: Cache, 
                       obj_file: Path, 
                       cache_key: str,
                       include_files: List[Path]) \
                           -> Tuple[int, str, str]:
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
        cache.statistics.record_cache_hit()

        # Determine depencency file path
        dep_file_path = obj_file.parent / f"{obj_file.name}.d"
        
        _safe_unlink(obj_file)
        _safe_unlink(dep_file_path)

        cached_artifacts = cache.get_entry(cache_key)
        assert cached_artifacts is not None

        copy_from_cache(cached_artifacts.obj_file_path, obj_file)
        
        # create depencency file
        _create_dep_file(dep_file_path, obj_file, include_files)
        
        trace("Finished. Exit code 0")
        return (
            0,
            expand_compile_output(cached_artifacts.stdout, StdStream.STDOUT),
            expand_compile_output(cached_artifacts.stderr, StdStream.STDERR),
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

            # remove the backslash line continuation characters
            buf = buf.replace("\\\n", "")

            # remove the trailing newline
            buf = buf[:-1]

            # split the remaining lines into a list
            lines = buf.split()

            # convert the list of strings into a list of Path objects
            return [Path(os.path.normpath(line)).absolute() for line in lines]

    return []
