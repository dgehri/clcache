import codecs
import multiprocessing
import os
from pathlib import Path
import re
import sys
import subprocess
from collections import defaultdict
from tempfile import TemporaryFile
from typing import Dict, List, Optional, Tuple, Union

from ..utils import trace
from ..config import CL_DEFAULT_CODEC


class AnalysisError(Exception):
    pass


class NoSourceFileError(AnalysisError):
    pass


class MultipleSourceFilesComplexError(AnalysisError):
    pass


class CalledForLinkError(AnalysisError):
    pass


class CalledWithPchError(AnalysisError):
    pass


class ExternalDebugInfoError(AnalysisError):
    pass


class CalledForPreprocessingError(AnalysisError):
    pass


class InvalidArgumentError(AnalysisError):
    pass


class CommandLineTokenizer:
    def __init__(self, content):
        self.argv = []
        self._content = content
        self._pos = 0
        self._token = ""
        self._parser = self._initialState

        while self._pos < len(self._content):
            self._parser = self._parser(self._content[self._pos])
            self._pos += 1

        if self._token:
            self.argv.append(self._token)

    def _initialState(self, currentChar):
        if currentChar.isspace():
            return self._initialState

        if currentChar == '"':
            return self._quotedState

        if currentChar == "\\":
            self._parseBackslash()
            return self._unquotedState

        self._token += currentChar
        return self._unquotedState

    def _unquotedState(self, currentChar):
        if currentChar.isspace():
            self.argv.append(self._token)
            self._token = ""
            return self._initialState

        if currentChar == '"':
            return self._quotedState

        if currentChar == "\\":
            self._parseBackslash()
            return self._unquotedState

        self._token += currentChar
        return self._unquotedState

    def _quotedState(self, currentChar):
        if currentChar == '"':
            return self._unquotedState

        if currentChar == "\\":
            self._parseBackslash()
            return self._quotedState

        self._token += currentChar
        return self._quotedState

    def _parseBackslash(self):
        numBackslashes = 0
        while self._pos < len(self._content) and self._content[self._pos] == "\\":
            self._pos += 1
            numBackslashes += 1

        followedByDoubleQuote = (
            self._pos < len(self._content) and self._content[self._pos] == '"'
        )
        if followedByDoubleQuote:
            self._token += "\\" * (numBackslashes // 2)
            if numBackslashes % 2 == 0:
                self._pos -= 1
            else:
                self._token += '"'
        else:
            self._token += "\\" * numBackslashes
            self._pos -= 1


def split_comands_file(content):
    return CommandLineTokenizer(content).argv


def expand_response_file(cmdline):
    '''
    Expand command line arguments that start with @ to the contents of the (response) file.
    '''
    ret = []

    for arg in cmdline:
        if len(arg) == 0:
            continue
        
        if arg[0] == "@":
            response_file = arg[1:]
            with open(response_file, "rb") as f:
                raw_bytes = f.read()

            encoding = None

            bom_to_encoding = {
                codecs.BOM_UTF32_BE: "utf-32-be",
                codecs.BOM_UTF32_LE: "utf-32-le",
                codecs.BOM_UTF16_BE: "utf-16-be",
                codecs.BOM_UTF16_LE: "utf-16-le",
            }

            for bom, enc in bom_to_encoding.items():
                if raw_bytes.startswith(bom):
                    encoding = enc
                    raw_bytes = raw_bytes[len(bom):]
                    break

            if encoding:
                response_file_content = raw_bytes.decode(encoding)
            else:
                response_file_content = raw_bytes.decode("UTF-8")

            ret.extend(
                expand_response_file(split_comands_file(response_file_content.strip()))
            )
        else:
            ret.append(arg)

    return ret


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


class Argument:
    def __init__(self, name):
        self.name = name

    def __len__(self):
        return len(self.name)

    def __str__(self):
        return f"/{self.name}"

    def __eq__(self, other):
        return type(self) == type(other) and self.name == other.name

    def __hash__(self):
        key = (type(self), self.name)
        return hash(key)


# /NAMEparameter (no space, required parameter).
class ArgumentT1(Argument):
    pass


# /NAME[parameter] (no space, optional parameter)
class ArgumentT2(Argument):
    pass


# /NAME[ ]parameter (optional space)
class ArgumentT3(Argument):
    pass


# /NAME parameter (required space)
class ArgumentT4(Argument):
    pass


class CommandLineAnalyzer:

    _args_with_params = {
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
        ArgumentT3("V"),
        ArgumentT3("imsvc"),
        ArgumentT3("external:I"),
        ArgumentT3("external:env"),
        # /NAME parameter
        ArgumentT4("Xclang"),
    }
    _args_with_params_sorted = sorted(
        _args_with_params, key=len, reverse=True)

    @staticmethod
    def _get_parametrized_arg_type(cmd_line_arg: str) -> Optional[Argument]:
        '''
        Get typed argument from command line argument.
        '''
        return next(
            (
                arg
                for arg in CommandLineAnalyzer._args_with_params_sorted
                if cmd_line_arg.startswith(arg.name, 1)
            ),
            None,
        )

    @staticmethod
    def parse_args_and_input_files(cmdline: List[str]) -> Tuple[Dict[str, List[str]], List[Path]]:
        '''
        Parse command line arguments and input files.

        Parameters:
            cmdline: The command line to parse.

        Returns:
            A tuple of two elements: 
            - The first element is a dictionary mapping argument names to a list of argument values. 
            - The second element is a list of input files.
        '''
        arguments = defaultdict(list)
        input_files: List[Path] = []
        i = 0
        while i < len(cmdline):
            arg_str: str = cmdline[i]

            # Plain arguments starting with / or -
            if arg_str.startswith("/") or arg_str.startswith("-"):
                arg = CommandLineAnalyzer._get_parametrized_arg_type(
                    arg_str)
                if arg is not None:
                    if isinstance(arg, ArgumentT1):
                        value = arg_str[len(arg) + 1:]
                        if not value:
                            raise InvalidArgumentError(
                                f"Parameter for {arg} must not be empty"
                            )
                    elif isinstance(arg, ArgumentT2):
                        value = arg_str[len(arg) + 1:]
                    elif isinstance(arg, ArgumentT3):
                        value = arg_str[len(arg) + 1:]
                        if not value:
                            value = cmdline[i + 1]
                            i += 1
                        elif value[0].isspace():
                            value = value[1:]
                    elif isinstance(arg, ArgumentT4):
                        value = cmdline[i + 1]
                        i += 1
                    else:
                        raise AssertionError("Unsupported argument type.")

                    arguments[arg.name].append(value)
                else:
                    # name not followed by parameter in this case
                    arg_name = arg_str[1:]
                    arguments[arg_name].append("")

            elif arg_str[0] == "@":
                raise AssertionError(
                    "No response file arguments (starting with @) must be left here."
                )

            else:
                input_files.append(Path(arg_str))

            i += 1

        return dict(arguments), input_files

    @staticmethod
    def analyze(cmdline: List[str]) -> Tuple[List[Tuple[Path, str]], List[Path]]:
        '''
        Analyzes the command line and returns a list of input and output files.

        Parameters:
            cmdline: The command line to analyze.

        Returns:
            A tuple of two lists. The first list contains tuples of input files 
            and their type (either /Tp or /Tc). 
            The second list contains output (object) files.
        '''

        options, orig_input_files = CommandLineAnalyzer.parse_args_and_input_files(
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
