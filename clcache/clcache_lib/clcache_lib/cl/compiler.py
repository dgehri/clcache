import multiprocessing
import os
import re
import subprocess
import sys
from pathlib import Path
from shutil import which
from tempfile import TemporaryFile
from typing import Dict, List, Optional, Set, Tuple

from ..config import CL_DEFAULT_CODEC
from ..utils import trace
from ..utils.args import (Argument, ArgumentNoParam, ArgumentT1, ArgumentT2, ArgumentT3,
                          ArgumentT4, CommandLineAnalyzer, split_comands_file)
from ..utils.errors import *


def extend_cmdline_from_env(cmd_line: List[str],
                            environment: Dict[str, str]) \
        -> Tuple[List[str], Dict[str, str]]:
    '''
    Extend command line with CL and _CL_ environment variables

    See https://learn.microsoft.com/en-us/cpp/build/reference/cl-environment-variables
    '''

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
            ArgumentT3("imsvc"),
            ArgumentT3("external:I"),
            ArgumentT3("external:env"),
            # /NAME parameter
            ArgumentT4("Xclang"),
        }

        args_to_unify_and_sort = [
            ("AI", True),
            ("I", True),
            ("FU", True),
            ("Fo", True),
            ("imsvc", True),
            ("external:I", True),
            ("external:env", False),
            ("Xclang", False),
            ("D", False),
            ("MD", False),
            ("MT", False),
            ("W0", False),
            ("W1", False),
            ("W2", False),
            ("W3", False),
            ("W4", False),
            ("Wall", False),
            ("Wv", False),
            ("WX", False),
            ("w1", False),
            ("w2", False),
            ("w3", False),
            ("w4", False),
            ("we", False),
            ("wo", False),
            ("wd", False),
            ("W", False),
            ("Z7", False),
            ("nologo", False),
            ("showIncludes", False)
        ]

        super().__init__(args=args_with_params, args_to_unify_and_sort=args_to_unify_and_sort)

    def analyze(self, cmdline: List[str]) -> Tuple[List[Tuple[Path, str]], List[Path]]:
        '''
        Analyzes the command line and returns a list of input and output files.

        Parameters:
            cmdline: The command line to analyze.

        Returns:
            A tuple of two lists. The first list contains tuples of input files 
            and their type (either /Tp or /Tc). 
            The second list contains output (object) files.
        '''

        options, orig_input_files = self.parse_args_and_input_files(
            cmdline)

        # Use an override pattern to shadow input files that have
        # already been specified in the function above
        input_file_dict = {f: "" for f in orig_input_files}
        compl = False
        if "Tp" in options:
            input_file_dict |= {Path(f): "/Tp" for f in options["Tp"]}
            compl = True
        if "Tc" in options:
            input_file_dict |= {Path(f): "/Tc" for f in options["Tc"]}
            compl = True

        # Now collect the inputFiles into the return format
        input_files = list(input_file_dict.items())
        if not input_files:
            raise NoSourceFileError()

        for opt in ["E", "EP", "P"]:
            if opt in options:
                raise CalledForPreprocessingError()

        # Technically, it would be possible to support /Zi: we'd just need to
        # copy the generated .pdb files into/out of the cache.
        if "Zi" in options:
            raise ExternalDebugInfoError()

        if "Yc" in options or "Yu" in options:
            raise CalledWithPchError()

        if "link" in options or "c" not in options:
            raise CalledForLinkError()

        if len(input_files) > 1 and compl:
            raise MultipleSourceFilesComplexError()

        obj_files = None
        prefix = Path()
        if "Fo" in options and options["Fo"][0]:
            # Handle user input
            tmp = Path(options["Fo"][0])
            if tmp.is_dir():
                prefix = tmp
            elif len(input_file_dict) == 1:
                obj_files = [tmp]
        if obj_files is None:
            # Generate from .c/.cpp filenames
            obj_files = [
                (prefix / f).with_suffix(".obj")
                for f, _ in input_files
            ]

        trace(f"Compiler source files: {input_files}")
        trace(f"Compiler object file: {obj_files}")
        return input_files, obj_files


def invoke_real_compiler(compiler_path: Path, cmd_line: List[str], capture_output: bool = False, environment: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
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


def job_count(cmd_line: List[str]) -> int:
    '''
    Returns the amount of jobs

    Returns the amount of jobs which should be run in parallel when 
    invoked in batch mode as determined by the /MP argument.
    '''
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


def find_compiler_binary() -> Optional[Path]:
    if "CLCACHE_CL" in os.environ:
        path: Path = Path(os.environ["CLCACHE_CL"])

        # If the path is not absolute, try to find it in the PATH
        if path.name == path:
            if p := which(path):
                path = Path(p)

        return path if path is not None and path.exists() else None

    return Path(p) if (p := which("cl.exe")) else None
