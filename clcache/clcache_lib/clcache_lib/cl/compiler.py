import codecs
import multiprocessing
import os
import re
import sys
import subprocess
from collections import defaultdict
from tempfile import TemporaryFile
from typing import List, Tuple

from ..utils import basename_without_extension, trace
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


def expand_cmdline(cmdline):
    ret = []

    for arg in cmdline:
        if arg[0] == "@":
            includeFile = arg[1:]
            with open(includeFile, "rb") as f:
                rawBytes = f.read()

            encoding = None

            bomToEncoding = {
                codecs.BOM_UTF32_BE: "utf-32-be",
                codecs.BOM_UTF32_LE: "utf-32-le",
                codecs.BOM_UTF16_BE: "utf-16-be",
                codecs.BOM_UTF16_LE: "utf-16-le",
            }

            for bom, enc in bomToEncoding.items():
                if rawBytes.startswith(bom):
                    encoding = enc
                    rawBytes = rawBytes[len(bom) :]
                    break

            if encoding:
                includeFileContents = rawBytes.decode(encoding)
            else:
                includeFileContents = rawBytes.decode("UTF-8")

            ret.extend(
                expand_cmdline(split_comands_file(includeFileContents.strip()))
            )
        else:
            ret.append(arg)

    return ret


def extend_cmdline_from_env(cmdLine, environment):
    remainingEnvironment = environment.copy()

    prependCmdLineString = remainingEnvironment.pop("CL", None)
    if prependCmdLineString is not None:
        cmdLine = split_comands_file(prependCmdLineString.strip()) + cmdLine

    appendCmdLineString = remainingEnvironment.pop("_CL_", None)
    if appendCmdLineString is not None:
        cmdLine = cmdLine + split_comands_file(appendCmdLineString.strip())

    return cmdLine, remainingEnvironment


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
    argumentsWithParameter = {
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
    argumentsWithParameterSorted = sorted(argumentsWithParameter, key=len, reverse=True)

    @staticmethod
    def _getParameterizedArgumentType(cmdLineArgument):
        return next(
            (
                arg
                for arg in CommandLineAnalyzer.argumentsWithParameterSorted
                if cmdLineArgument.startswith(arg.name, 1)
            ),
            None,
        )

    @staticmethod
    def parseArgumentsAndInputFiles(cmdline):
        arguments = defaultdict(list)
        inputFiles = []
        i = 0
        while i < len(cmdline):
            cmdLineArgument = cmdline[i]

            # Plain arguments starting with / or -
            if cmdLineArgument.startswith("/") or cmdLineArgument.startswith("-"):
                arg = CommandLineAnalyzer._getParameterizedArgumentType(cmdLineArgument)
                if arg is not None:
                    if isinstance(arg, ArgumentT1):
                        value = cmdLineArgument[len(arg) + 1 :]
                        if not value:
                            raise InvalidArgumentError(
                                f"Parameter for {arg} must not be empty"
                            )
                    elif isinstance(arg, ArgumentT2):
                        value = cmdLineArgument[len(arg) + 1 :]
                    elif isinstance(arg, ArgumentT3):
                        value = cmdLineArgument[len(arg) + 1 :]
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
                    argumentName = cmdLineArgument[1:]
                    arguments[argumentName].append("")

            elif cmdLineArgument[0] == "@":
                raise AssertionError(
                    "No response file arguments (starting with @) must be left here."
                )

            else:
                inputFiles.append(cmdLineArgument)

            i += 1

        return dict(arguments), inputFiles

    @staticmethod
    def analyze(cmdline: List[str]) -> Tuple[List[Tuple[str, str]], List[str]]:
        options, inputFiles = CommandLineAnalyzer.parseArgumentsAndInputFiles(cmdline)
        # Use an override pattern to shadow input files that have
        # already been specified in the function above
        inputFiles = {inputFile: "" for inputFile in inputFiles}
        compl = False
        if "Tp" in options:
            inputFiles |= {inputFile: "/Tp" for inputFile in options["Tp"]}
            compl = True
        if "Tc" in options:
            inputFiles |= {inputFile: "/Tc" for inputFile in options["Tc"]}
            compl = True

        # Now collect the inputFiles into the return format
        inputFiles = list(inputFiles.items())
        if not inputFiles:
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

        if len(inputFiles) > 1 and compl:
            raise MultipleSourceFilesComplexError()

        objectFiles = None
        prefix = ""
        if "Fo" in options and options["Fo"][0]:
            # Handle user input
            tmp = os.path.normpath(options["Fo"][0])
            if os.path.isdir(tmp):
                prefix = tmp
            elif len(inputFiles) == 1:
                objectFiles = [tmp]
        if objectFiles is None:
            # Generate from .c/.cpp filenames
            objectFiles = [
                f"{os.path.join(prefix, basename_without_extension(f))}.obj"
                for f, _ in inputFiles
            ]

        trace(f"Compiler source files: {inputFiles}")
        trace(f"Compiler object file: {objectFiles}")
        return inputFiles, objectFiles


def invoke_real_compiler(
    compilerBinary, cmdLine, captureOutput=False, outputAsString=True, environment=None
):
    realCmdline = [compilerBinary] + cmdLine

    # if command line longer than 32767 chars, use a response file
    # See https://devblogs.microsoft.com/oldnewthing/20031210-00/?p=41553
    if len(" ".join(realCmdline)) >= 32000:  # keep some chars as a safety margin
        with TemporaryFile(mode="wt", suffix=".rsp") as rspFile:
            rspFile.writelines(" ".join(cmdLine) + "\n")
            rspFile.flush()
            return invoke_real_compiler(
                compilerBinary,
                [f"@{os.path.realpath(rspFile.name)}"],
                captureOutput,
                outputAsString,
                environment,
            )

    trace(f"Invoking real compiler as {realCmdline}")

    environment = environment or os.environ

    # Environment variable set by the Visual Studio IDE to make cl.exe write
    # Unicode output to named pipes instead of stdout. Unset it to make sure
    # we can catch stdout output.
    environment.pop("VS_UNICODE_OUTPUT", None)

    returnCode = None
    stdout = b""
    stderr = b""
    if captureOutput:
        # Don't use subprocess.communicate() here, it's slow due to internal
        # threading.
        with TemporaryFile() as stdoutFile, TemporaryFile() as stderrFile:
            compilerProcess = subprocess.Popen(
                realCmdline, stdout=stdoutFile, stderr=stderrFile, env=environment
            )
            returnCode = compilerProcess.wait()
            stdoutFile.seek(0)
            stdout = stdoutFile.read()
            stderrFile.seek(0)
            stderr = stderrFile.read()
    else:
        sys.stdout.flush()
        sys.stderr.flush()
        returnCode = subprocess.call(realCmdline, env=environment)

    trace("Real compiler returned code {0:d}".format(returnCode))

    if outputAsString:
        stdoutString = stdout.decode(CL_DEFAULT_CODEC)
        stderrString = stderr.decode(CL_DEFAULT_CODEC)
        return returnCode, stdoutString, stderrString

    return returnCode, stdout, stderr


# Returns the amount of jobs which should be run in parallel when
# invoked in batch mode as determined by the /MP argument
def job_count(cmdLine):
    mpSwitches = [arg for arg in cmdLine if re.match(r"^/MP(\d+)?$", arg)]
    if not mpSwitches:
        return 1

    # the last instance of /MP takes precedence
    mpSwitch = mpSwitches.pop()

    count = mpSwitch[3:]
    if count != "":
        return int(count)

    # /MP, but no count specified; use CPU count
    try:
        return multiprocessing.cpu_count()
    except NotImplementedError:
        # not expected to happen
        return 2
