import concurrent.futures
import multiprocessing
import os
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Iterator
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryFile

from ..cache import *  # type: ignore
from ..cache.cache import Cache, ensure_artifacts_exist
from ..cache.file_cache import ManifestRepository
from ..cache.manifest_entry import create_manifest_entry
from ..cache.stats import MissReason
from ..cache.virt import (
    StdStream,
    canonicalize_path,
    expand_compile_output,
    set_llvm_dir,
)
from ..config import CL_DEFAULT_CODEC
from ..utils.args import (
    ArgumentT1,
    ArgumentT2,
    ArgumentT3,
    ArgumentT4,
    CommandLineAnalyzer,
    expand_response_file,
    split_comands_file,
)
from ..utils.errors import *
from ..utils.util import line_iter, print_stdout_and_stderr


def _sanitize_stdout(output: str) -> str:
    # iterate over stdout and collapse folders if path is too long
    regex = re.compile(r"^(\w+: [ \w]+: +)(\S.*?)\r?$")

    lines = []
    for line in line_iter(output):
        if m := regex.match(line):
            file_path = m[2]
            if len(file_path) > MAX_PATH - 10:
                sanitized_path = os.path.normpath(file_path)
                lines.append(f"{m[1]}{sanitized_path}{line[m.end(2):]}")
                continue

        lines.append(line)

    # print stdout to console
    return "".join(lines)


def is_disabled() -> bool:
    return "CLCACHE_DISABLE" in os.environ

def _invoke_real_compiler(
    compiler_path: Path, cmd_line: list[str], disable_auto_rsp=False
) -> int:
    """Invoke the real compiler and return its exit code."""
    set_llvm_dir(compiler_path)

    real_cmd_line = [str(compiler_path)] + cmd_line

    # if command line longer than 32767 chars, use a response file
    # See https://devblogs.microsoft.com/oldnewthing/20031210-00/?p=41553
    if (
        not disable_auto_rsp and len(" ".join(real_cmd_line)) >= 32000
    ):  # keep some chars as a safety margin
        with NamedTemporaryFile(mode="wt", suffix=".rsp", delete=False) as rsp_file:
            rsp_file.write(" ".join(cmd_line) + "\n")
            temp_file_path = rsp_file.name  # Save the path before the file is closed

        result = _invoke_real_compiler(
            compiler_path,
            [f"@{os.path.realpath(temp_file_path)}"],
            disable_auto_rsp=True,
        )

        os.remove(temp_file_path)
        return result

    log(f"Invoking compiler: {' '.join(real_cmd_line)}")

    # Environment variable set by the Visual Studio IDE to make cl.exe write
    # Unicode output to named pipes instead of stdout. Unset it to make sure
    # we can catch stdout output.
    environment = dict(os.environ)
    environment.pop("VS_UNICODE_OUTPUT", None)

    sys.stderr.flush()
    return subprocess.call(real_cmd_line, env=environment)


def _capture_real_compiler(
    compiler_path: Path,
    cmd_line: list[str],
    environment: dict[str, str] | None = None,
    disable_auto_rsp=False,
) -> tuple[int, str, str]:
    """Invoke the real compiler and return its exit code, stdout and stderr."""

    set_llvm_dir(compiler_path)

    read_cmd_line = [str(compiler_path)] + cmd_line

    # if command line longer than 32767 chars, use a response file
    # See https://devblogs.microsoft.com/oldnewthing/20031210-00/?p=41553
    if (
        not disable_auto_rsp and len(" ".join(read_cmd_line)) >= 32000
    ):  # keep some chars as a safety margin
        with NamedTemporaryFile(mode="wt", suffix=".rsp", delete=False) as rsp_file:
            rsp_file.write(" ".join(cmd_line) + "\n")
            temp_file_path = rsp_file.name  # Save the path before the file is closed

        result = _capture_real_compiler(
            compiler_path,
            [f"@{os.path.realpath(temp_file_path)}"],
            environment,
            disable_auto_rsp=True,
        )

        os.remove(temp_file_path)
        return result

    log(f"Invoking compiler: {' '.join(read_cmd_line)}")

    environment = environment or dict(os.environ)

    # Environment variable set by the Visual Studio IDE to make cl.exe write
    # Unicode output to named pipes instead of stdout. Unset it to make sure
    # we can catch stdout output.
    environment.pop("VS_UNICODE_OUTPUT", None)

    return_code: int = -1

    # Don't use subprocess.communicate() here, it's slow due to internal
    # threading.
    with TemporaryFile() as stdout_file, TemporaryFile() as stderr_file:
        compiler_process = subprocess.Popen(
            read_cmd_line, stdout=stdout_file, stderr=stderr_file, env=environment
        )
        return_code = compiler_process.wait()
        stdout_file.seek(0)
        stdout = stdout_file.read().decode(CL_DEFAULT_CODEC)
        stderr_file.seek(0)
        stderr = stderr_file.read().decode(CL_DEFAULT_CODEC)

    log("Real compiler returned code: {:d}".format(return_code), force_flush=True)
    return return_code, _sanitize_stdout(stdout), stderr


def process_compile_request(cache: Cache, compiler_path: Path, args: list[str]) -> int:
    """
    Process a compile request.

    Returns:
        The exit code of the compiler.
    """
    log("Command line: '{!s}'".format(" ".join(args)))

    set_llvm_dir(compiler_path)

    cmdline, environment = _extend_cmdline_from_env(args, dict(os.environ))
    cmdline = expand_response_file(cmdline)
    log("Expanded commandline: '{!s}'".format(" ".join(cmdline)))

    try:
        analyzer = ClCommandLineAnalyzer()
        src_files, obj_files = analyzer.analyze(cmdline)
        return _schedule_jobs(
            cache, compiler_path, cmdline, environment, src_files, obj_files, analyzer
        )
    except InvalidArgumentError:
        log(f"Cannot cache invocation as {cmdline}: invalid argument", LogLevel.ERROR)
        cache.statistics.record_cache_miss(MissReason.CALL_WITH_INVALID_ARGUMENT)
    except NoSourceFileError:
        log(f"Cannot cache invocation as {cmdline}: no source file found")
        cache.statistics.record_cache_miss(MissReason.CALL_WITHOUT_SOURCE_FILE)
    except MultipleSourceFilesComplexError:
        log(f"Cannot cache invocation as {cmdline}: multiple source files found")
        cache.statistics.record_cache_miss(MissReason.CALL_WITH_MULTIPLE_SOURCE_FILES)
    except CalledWithPchError:
        log(f"Cannot cache invocation as {cmdline}: precompiled headers in use")
        cache.statistics.record_cache_miss(MissReason.CALL_WITH_PCH)
    except CalledForLinkError:
        log(f"Cannot cache invocation as {cmdline}: called for linking")
        cache.statistics.record_cache_miss(MissReason.CALL_FOR_LINKING)
    except ExternalDebugInfoError:
        log(
            f"Cannot cache invocation as {cmdline}: external debug information (/Zi) is not supported"
        )
        cache.statistics.record_cache_miss(MissReason.CALL_FOR_EXTERNAL_DEBUG_INFO)
    except CalledForPreprocessingError:
        log(f"Cannot cache invocation as {cmdline}: called for preprocessing")
        cache.statistics.record_cache_miss(MissReason.CALL_FOR_PREPROCESSING)

    return _invoke_real_compiler(compiler_path, args)


def _extend_cmdline_from_env(
    cmd_line: list[str], environment: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    """
    Extend command line with CL and _CL_ environment variables

    See https://learn.microsoft.com/en-us/cpp/build/reference/cl-environment-variables
    """

    _env = environment.copy()

    prefix = _env.pop("CL", None)
    if prefix is not None:
        cmd_line = split_comands_file(prefix.strip()) + cmd_line

    postfix = _env.pop("_CL_", None)
    if postfix is not None:
        cmd_line += split_comands_file(postfix.strip())

    return cmd_line, _env


class ClCommandLineAnalyzer(CommandLineAnalyzer):
    def __init__(self):
        args_with_params = {
            # /NAMEparameter
            ArgumentT1("Ob"),
            ArgumentT1("Yl"),
            ArgumentT1("Zm"),
            # /NAME[parameter]
            ArgumentT2("doc"),
            ArgumentT2("FA"),
            ArgumentT2("FR"),
            ArgumentT2("Fr"),
            ArgumentT2("Gs"),
            ArgumentT2("MP"),
            ArgumentT2("Yc"),
            ArgumentT2("Yu"),
            ArgumentT2("Zp"),
            ArgumentT2("Fa"),
            ArgumentT2("Fd"),
            ArgumentT2("Fe"),
            ArgumentT2("Fi"),
            ArgumentT2("Fm"),
            ArgumentT2("Fo"),
            ArgumentT2("Fp"),
            ArgumentT2("Wv"),
            ArgumentT2("experimental:external"),
            ArgumentT2("external:anglebrackets"),
            ArgumentT2("external:W"),
            ArgumentT2("external:templates"),
            # /NAME[ ]parameter
            ArgumentT3("AI"),
            ArgumentT3("D"),
            ArgumentT3("Tc"),
            ArgumentT3("Tp"),
            ArgumentT3("FI"),
            ArgumentT3("U"),
            ArgumentT3("I"),
            ArgumentT3("F"),
            ArgumentT3("FU"),
            ArgumentT3("w1"),
            ArgumentT3("w2"),
            ArgumentT3("w3"),
            ArgumentT3("w4"),
            ArgumentT3("wd"),
            ArgumentT3("we"),
            ArgumentT3("wo"),
            ArgumentT3("W"),
            ArgumentT3("V"),
            ArgumentT3("imsvc", mapped_name="I"),
            ArgumentT3("external:I", mapped_name="I"),
            ArgumentT3(
                "external:env:",
                mapped_name="I",
                getter=lambda value: os.environ.get(value, "").split(";"),
            ),  # type: ignore
            # /NAME parameter
            ArgumentT4("Xclang"),
        }

        args_to_unify_and_sort = [
            ("AI", True),
            ("I", True),
            ("FU", True),
            ("Fd", True),
            ("imsvc", True),
            ("external:I", True),
            ("Tp", True),
            ("Tc", True),
            ("Fo", True),
            ("external:env", False),
            ("TP", False),
            ("TC", False),
            ("D", False),
            ("MD", False),
            ("MT", False),
            ("Z7", False),
            ("nologo", False),
            ("showIncludes", False),
        ]

        super().__init__(
            args=args_with_params, args_to_unify_and_sort=args_to_unify_and_sort
        )

    def analyze(self, cmdline: list[str]) -> tuple[list[tuple[Path, str]], list[Path]]:
        """
        Analyzes the command line and returns a list of input and output files.

        Parameters:
            cmdline: The command line to analyze.

        Returns:
            A tuple of two lists. The first list contains tuples of input files
            and their type (either /Tp or /Tc).
            The second list contains output (object) files.
        """

        args, orig_input_files = self.parse_args_and_input_files(cmdline)

        # Use an override pattern to shadow input files that have
        # already been specified in the function above
        input_file_dict = {f: "" for f in orig_input_files}
        compl = False
        if "Tp" in args:
            input_file_dict |= {Path(f): "/Tp" for f in args["Tp"]}
            compl = True
        if "Tc" in args:
            input_file_dict |= {Path(f): "/Tc" for f in args["Tc"]}
            compl = True

        # Now collect the input_file_dict into the return format
        input_files = list(input_file_dict.items())
        if not input_files:
            raise NoSourceFileError()

        for opt in ["E", "EP", "P"]:
            if opt in args:
                raise CalledForPreprocessingError()

        # Technically, it would be possible to support /Zi: we'd just need to
        # copy the generated .pdb files into/out of the cache.
        if "Zi" in args:
            raise ExternalDebugInfoError()

        if "Yc" in args or "Yu" in args:
            raise CalledWithPchError()

        if "link" in args or "c" not in args:
            raise CalledForLinkError()

        if len(input_files) > 1 and compl:
            raise MultipleSourceFilesComplexError()

        obj_files = None
        output_folder = Path()
        if "Fo" in args and args["Fo"][0]:
            # Determine output file name from /Fo option
            path_name = Path(args["Fo"][0])
            if path_name.is_dir():
                output_folder = path_name
            elif len(input_file_dict) == 1:
                obj_files = [path_name]

        if not obj_files:
            # Generate from .c/.cpp filenames
            obj_files = [
                (output_folder / f).with_suffix(".obj") for f, _ in input_files
            ]

        log(f"Input files: {';'.join([str(f) for f, _ in input_files])}")
        log(f"Output files: {';'.join([str(f) for f in obj_files])}")
        return input_files, obj_files


def _job_count(cmd_line: list[str]) -> int:
    """
    Returns the amount of jobs

    Returns the amount of jobs which should be run in parallel when
    invoked in batch mode as determined by the /MP argument.
    """
    mp_switches = [arg for arg in cmd_line if re.match(r"^/MP(\d+)?$", arg)]
    if not mp_switches:
        return 1

    # The last instance of /MP takes precedence
    mp_switch = mp_switches.pop()

    # Get count from /MP:count
    count = mp_switch[3:]
    if count != "":
        return int(count)

    # /MP, but no count specified; use CPU count
    try:
        return multiprocessing.cpu_count()
    except NotImplementedError:
        # not expected to happen
        return 2


def _process_cache_hit(
    cache: Cache, obj_file: Path, cache_key: str
) -> tuple[int, str, str]:
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
    log(f"Reusing cached object for key {cache_key} for object file {obj_file}")

    with cache.lock_for(cache_key):
        cache.statistics.record_cache_hit()

        if obj_file.exists():
            success = False
            for _ in range(60):
                try:
                    obj_file.unlink()
                    success = True
                    break
                except Exception:
                    log(
                        f"Failed to delete object file {obj_file}, retrying...",
                        LogLevel.WARN,
                    )
                    time.sleep(1)

            if not success:
                obj_file.unlink()

        cached_artifacts = cache.get_entry(cache_key)
        assert cached_artifacts is not None

        copy_from_cache(cached_artifacts.payload_path, obj_file)
        return (
            0,
            expand_compile_output(cached_artifacts.stdout, StdStream.STDOUT),
            expand_compile_output(cached_artifacts.stderr, StdStream.STDERR),
        )


def _filter_source_files(
    cmd_line: list[str], src_files: list[tuple[Path, str]]
) -> Iterator[str]:
    """
    Filter out all source files from the command line

        Parameters:
            cmd_line: The command line to filter.
            src_files: The source files to filter.

        Returns:
            An iterator over the filtered command line.
    """
    set_of_sources = {str(src_file) for src_file, _ in src_files}
    skipped_args = ("/Tc", "/Tp", "-Tp", "-Tc")
    yield from (
        arg
        for arg in cmd_line
        if not (arg in set_of_sources or arg.startswith(skipped_args))
    )


def _schedule_jobs(
    cache: Cache,
    compiler: Path,
    cmd_line: list[str],
    environment: dict[str, str],
    src_files: list[tuple[Path, str]],
    obj_files: list[Path],
    analyzer: ClCommandLineAnalyzer,
) -> int:
    """
    Schedule jobs for the given command line.

    Parameters:
        cache: The cache to use.
        compiler: The compiler to use.
        cmd_line: The command line to process.
        environment: The environment to use.
        src_files: The source files to process. Each tuple contains the path to the source file and the language.
        obj_files: The object files to process.
    """
    # Filter out all source files from the command line to form base_cmdline
    base_cmdline = [
        arg
        for arg in _filter_source_files(cmd_line, src_files)
        if not arg.startswith("/MP")
    ]

    exit_code: int = 0

    if (len(src_files) == 1 and len(obj_files) == 1) or os.getenv("CLCACHE_SINGLEFILE"):
        assert len(src_files) == 1
        assert len(obj_files) == 1
        src_file, src_language = src_files[0]
        obj_file = obj_files[0]
        job_cmdline = base_cmdline + [src_language + str(src_file)]
        exit_code, out, err = _process_single_source(
            cache, compiler, job_cmdline, src_file, obj_file, environment, analyzer
        )
        log(f"Finished. Exit code {exit_code}", force_flush=True)
        print_stdout_and_stderr(out, err, CL_DEFAULT_CODEC)
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=_job_count(cmd_line)
        ) as executor:
            jobs = []
            for (src_file, src_language), obj_file in zip(src_files, obj_files):
                job_cmdline = base_cmdline + [src_language + str(src_file)]
                jobs.append(
                    executor.submit(
                        _process_single_source,
                        cache,
                        compiler,
                        job_cmdline,
                        src_file,
                        obj_file,
                        environment,
                        analyzer,
                    )
                )
            for future in concurrent.futures.as_completed(jobs):
                exit_code, out, err = future.result()
                log("Finished. Exit code {:d}".format(exit_code), force_flush=True)
                print_stdout_and_stderr(out, err, CL_DEFAULT_CODEC)

                if exit_code != 0:
                    break

    return exit_code


def _process_single_source(
    cache: Cache,
    compiler: Path,
    cmdline: list[str],
    src_file: Path,
    obj_file: Path,
    environment: dict[str, str] | None,
    analyzer: ClCommandLineAnalyzer,
) -> tuple[int, str, str]:
    """
    Process a single source file.

        Parameters:
            cache (Cache): The cache to use.
            compiler (Path): The path to the compiler.
            cmdline (list[str]): The command line to invoke the compiler with.
            src_file (Path): The source file to compile.
            obj_file (Path): The object file to create.
            environment (Dict[str, str]): The environment to use when invoking the compiler.

        Returns:
            tuple[int, str, str]: A tuple containing the exit code, stdout, stderr
    """
    try:
        assert obj_file is not None
        return _process(cache, obj_file, compiler, cmdline, src_file, analyzer)

    except CompilerFailedException as e:
        return e.get_compiler_result()
    except Exception as e:
        # format exception with full call stack to string
        log(f"Exception occurred: {traceback.format_exc()}", LogLevel.ERROR)
        return _capture_real_compiler(compiler, cmdline, environment=environment)


def _process(
    cache: Cache,
    obj_file: Path,
    compiler_path: Path,
    cmdline: list[str],
    src_file: Path,
    analyzer: ClCommandLineAnalyzer,
) -> tuple[int, str, str]:
    """
    Process a single source file.

        Parameters:
            cache (Cache): The cache to use.
            obj_file (Path): The object file to create.
            compiler (Path): The path to the compiler.
            src_file (Path): The source file to compile.

        Returns:
            tuple[int, str, str]: A tuple containing the exit code, stdout, stderr
    """

    # Get manifest hash
    manifest_hash: str = _get_manifest_hash(compiler_path, cmdline, src_file, analyzer)

    # Acquire lock for manifest hash to prevent two jobs from compiling the same source
    # file at the same time. This is a frequent situation on Jenkins, and having the 2nd
    # job wait for the 1st job to finish compiling the source file is more efficient overall.
    with FileLock(manifest_hash, 120 * 1000 * 1000):
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
                                    [expand_path(path) for path in entry.includeFiles]
                                )
                            )

                            # log(f"Includes content hash: {includes_content_hash}")

                            # Check if include files have changed, if so, skip this entry
                            if entry.includesContentHash != includes_content_hash:
                                continue

                            # Include files have not changed, we have a hit!
                            cache_key = entry.objectHash
                            manifest_hit = True

                            # Check if object file exists in cache
                            with cache.lock_for(cache_key):
                                hit = cache.has_entry(cache_key)
                                if hit:
                                    # Move manifest entry to the top of the entries in the manifest
                                    # (if not already at top), so that we can use LRU replacement
                                    if entry_index > 0:
                                        log("Moving manifest entry to top of manifest")
                                        manifest.touch_entry(cache_key)
                                        cache.set_manifest(manifest_hash, manifest)

                                    # Object cache hit!
                                    return _process_cache_hit(
                                        cache, obj_file, cache_key
                                    )

                    miss_reason = MissReason.HEADER_CHANGED_MISS
                else:
                    miss_reason = MissReason.SOURCE_CHANGED_MISS
            except Exception:
                log(f"Exception occurred: {traceback.format_exc()}", LogLevel.ERROR)
                cache.statistics.record_cache_miss(MissReason.CACHE_FAILURE)
                raise

        # If we get here, we have a cache miss and we'll need to invoke the real compiler
        if manifest_hit:
            log("Manifest entry hit, but no object file found in cache")
            # Got a manifest, but no object => invoke real compiler
            compiler_result = _capture_real_compiler(compiler_path, cmdline)

            with cache.manifest_lock_for(manifest_hash):
                assert cache_key is not None
                return ensure_artifacts_exist(
                    cache,
                    cache_key,
                    miss_reason,
                    obj_file,
                    compiler_result,
                    canonicalize_stdout=lambda s: canonicalize_compile_output(
                        s, StdStream.STDOUT
                    ),
                    canonicalize_stderr=lambda s: canonicalize_compile_output(
                        s, StdStream.STDERR
                    ),
                )
        else:
            log("Manifest entry miss, invoking real compiler")
            # Also generate manifest
            strip_includes = False
            if "/showIncludes" not in cmdline:
                # Ensure compiler dumps include files, but strip them
                # before printing to stdout, unless /showIncludes is used
                cmdline = list(cmdline)
                cmdline.insert(0, "/showIncludes")
                strip_includes = True

            # Invoke real compiler and get output
            exit_code, compiler_out, compiler_err = _capture_real_compiler(
                compiler_path, cmdline
            )

            # Create manifest entry
            include_paths, stripped_compiler_out = _parse_includes_set(
                compiler_out, src_file, strip_includes
            )
            compiler_result = (exit_code, stripped_compiler_out, compiler_err)

            entry = create_manifest_entry(manifest_hash, include_paths)
            cache_key = entry.objectHash

            def add_manifest() -> int:
                with cache.manifest_lock_for(manifest_hash):
                    if manifest_info := cache.get_manifest(
                        manifest_hash, skip_remote=True
                    ):
                        manifest, old_size = manifest_info
                    else:
                        manifest = Manifest()
                        old_size = 0

                    manifest.add_entry(entry)
                    new_size = cache.set_manifest(
                        manifest_hash, manifest, Location.LOCAL
                    )

                # Setting remote manifest outside lock
                cache.set_manifest(manifest_hash, manifest, Location.REMOTE)
                return new_size - old_size

            return ensure_artifacts_exist(
                cache,
                cache_key,
                miss_reason,
                obj_file,
                compiler_result,
                canonicalize_stdout=lambda s: canonicalize_compile_output(
                    s, StdStream.STDOUT
                ),
                canonicalize_stderr=lambda s: canonicalize_compile_output(
                    s, StdStream.STDERR
                ),
                post_commit_action=add_manifest,
            )


def _get_manifest_hash(
    compiler_path: Path,
    cmd_line: list[str],
    src_file: Path,
    analyzer: ClCommandLineAnalyzer,
) -> str:
    """
    Returns a hash of the manifest file that would be used for the given command line.
    """
    compiler_hash = get_compiler_hash(compiler_path)

    args, input_files = analyzer.parse_args_and_input_files(cmd_line)

    # Append the content of the INCLUDE environment variable to the list of include files
    # to ensure that the manifest hash is different if the INCLUDE environment variable
    # changes.
    include_env = os.environ.get("INCLUDE", "")
    args["I"].extend(include_env.split(";"))
    log(f"INCLUDE env var: {include_env}", LogLevel.TRACE)

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

    cmd_line.extend(canonicalize_path_arg(value) for value in input_files)

    toolset_data = "{}|{}|{}".format(
        compiler_hash, cmd_line, ManifestRepository.MANIFEST_FILE_CL_FORMAT_VERSION
    )
    log(f"Toolset hash data: {toolset_data}", LogLevel.TRACE)
    return get_file_hash(src_file, toolset_data)


def _parse_includes_set(
    compiler_output: str, src_file: Path, strip: bool
) -> tuple[list[Path], str]:
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
    include_set: set[Path] = set()

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
    regex = re.compile(r"^\w+: [ \w]+: +(\S.*?)\r?$")

    abs_src_file = src_file.absolute()
    for line in line_iter(compiler_output):
        if m := regex.match(line):
            file_path = Path(os.path.normpath(m[1])).absolute()
            if file_path != abs_src_file:
                include_set.add(file_path)
        elif strip:
            filtered_output.append(line)

    if strip:
        return list(include_set), "".join(filtered_output)
    else:
        return list(include_set), compiler_output
