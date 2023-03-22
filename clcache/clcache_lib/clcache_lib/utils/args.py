
import codecs
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ..utils.errors import *


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


def expand_response_file(cmdline : List[str]) -> List[str]:
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
                expand_response_file(split_comands_file(
                    response_file_content.strip()))
            )
        else:
            ret.append(arg)

    return ret


class Argument:
    def __init__(self, name):
        self.name = name

    def __len__(self):
        return len(self.name)

    def __str__(self):
        return f"/{self.name}"

    def __eq__(self, other):
        return type(self) == type(other) \
            and self.name == other.name

    def __hash__(self):
        key = (type(self), self.name)
        return hash(key)


class ArgumentNoParam(Argument):
    '''/NAME (no space)'''
    pass


class ArgumentT1(Argument):
    '''/NAMEparameter (no space, required parameter).'''
    pass


class ArgumentT2(Argument):
    '''/NAME[parameter] (no space, optional parameter)'''
    pass


class ArgumentT3(Argument):
    '''/NAME[ ]parameter (optional space)'''
    pass


class ArgumentT4(Argument):
    '''/NAME parameter (required space)'''
    pass


class ArgumentQtShort(Argument):
    '''-<LETTER> (short option)'''

    def __str__(self):
        return f"-{self.name}"


class ArgumentQtLong(Argument):
    '''--<NAME> (long option)'''

    def __str__(self):
        return f"--{self.name}"


class ArgumentQtShortWithParam(Argument):
    '''-<LETTER>[= ]<ARG> (short option)'''

    def __str__(self):
        return f"-{self.name}"


class ArgumentQtLongWithParam(Argument):
    '''--<NAME>[= ]<ARG> (long option)'''

    def __str__(self):
        return f"--{self.name}"


class CommandLineAnalyzer:

    def __init__(self, args: Set[Argument],
                 args_to_unify_and_sort: List[Tuple[str, bool]]) -> None:
        self._args = sorted(
            args, key=len, reverse=True)
        self._args_to_unify_and_sort = args_to_unify_and_sort

    def _get_parametrized_arg_type(self, cmd_line_arg: str) -> Optional[Argument]:
        '''
        Get typed argument from command line argument.
        '''
        for arg in self._args:
            offset = 1

            if isinstance(arg, (ArgumentQtLongWithParam, ArgumentQtLong)) \
                    and cmd_line_arg.startswith("--"):
                offset = 2

            if cmd_line_arg.startswith(arg.name, offset):
                return arg

    def get_args_to_unify_and_sort(self) -> Dict[str, bool]:
        # Convert list of tuples to dictionary
        return dict(self._args_to_unify_and_sort)

    def _parse_arg(self,
                   arg: Argument,
                   cmdline: List[str], i: int) \
            -> Tuple[int, Optional[str]]:
        # sourcery skip: assign-if-exp
        '''
        Parse argument.

        Parameters:
            arg: The argument to parse.
            cmdline: The command line to parse.
            i: The index of the argument to parse.

        Returns:
            A tuple of two elements:
            - The first element is the index of the next argument to parse.
            - The second element is the value of the argument.
        '''
        if isinstance(arg, ArgumentNoParam):
            # /NAME (no space)
            return i + 1, None

        if isinstance(arg, ArgumentT1):
            # /NAMEparameter (no space, required parameter).
            if value := cmdline[i][len(str(arg)):]:
                return i + 1, value
            raise InvalidArgumentError(f"Missing parameter for {arg}")

        if isinstance(arg, ArgumentT2):
            # /NAME[parameter] (no space, optional parameter)
            return i + 1, cmdline[i][len(str(arg)):]

        if isinstance(arg, ArgumentT3):
            # /NAME[ ]parameter (optional space)
            value = cmdline[i][len(str(arg)):]
            if not value:
                return i + 2, cmdline[i + 1]
            if value[0].isspace():
                return i + 1, value[1:]
            else:
                return i + 1, value

        if isinstance(arg, ArgumentT4):
            # /NAME parameter (required space)
            return i + 2, cmdline[i + 1]

        if isinstance(arg, (ArgumentQtShort, ArgumentQtLong)):
            # -<LETTER> (short option)
            # --<NAME> (long option)
            return i + 1, None

        if isinstance(arg, (ArgumentQtShortWithParam, ArgumentQtLongWithParam)):
            # -<LETTER>[= ]<ARG> (short option)
            # --<NAME>[= ]<ARG> (long option)
            value = cmdline[i][len(str(arg)):]
            if not value:
                return i + 2, cmdline[i + 1]
            elif value[0] == "=":
                value = value[1:]
            return i + 1, value

        raise AssertionError("Unsupported argument type.")

    def parse_args_and_input_files(self, cmdline: List[str]) \
            -> Tuple[Dict[str, List[str]], List[Path]]:

        # Iterate over command line arguments and use _parse_arg to parse them.
        arguments = defaultdict(list)
        input_files: List[Path] = []
        i = 0
        while i < len(cmdline):
            arg_str: str = cmdline[i]

            # Parse argument if starting with slash or dash
            if arg_str.startswith(('/', '-')):
                if arg := self._get_parametrized_arg_type(arg_str):
                    i, value = self._parse_arg(arg, cmdline, i)
                    arguments[arg.name].append(value)
                else:
                    # name not followed by parameter in this case
                    arguments[arg_str[1:]].append(None)
                    i += 1
            elif arg_str.startswith('@'):
                raise AssertionError(
                    "Response file must be expanded before parsing.")
            else:
                input_files.append(Path(arg_str))
                i += 1

        return arguments, input_files
