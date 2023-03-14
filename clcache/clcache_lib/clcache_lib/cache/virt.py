import contextlib
import functools
import os
import re
from enum import Enum
from pathlib import Path
from typing import List, Optional

from ..utils import (get_long_path_name, get_short_path_name, line_iter,
                     line_iter_b, resolve)
from .ex import LogicException

# The folowing replacement strings are used to canonicalize paths in the cache.
BASEDIR_REPLACEMENT: str = "<BASE_DIR>"
BUILDDIR_REPLACEMENT: str = "<BUILD_DIR>"
CONANDIR_REPLACEMENT: str = "<CONAN_USER_HOME>"
QTDIR_REPLACEMENT: str = "<QT_DIR>"
LLVM_REPLACEMENT: str = "<LLVM_DIR>"


class StdStream(Enum):
    STDOUT = 1
    STDERR = 2


def expand_compile_output(compiler_output: str, stream: StdStream) -> str:
    """Expand the canonicalized paths in the compiler output."""
    regex = RE_STDOUT if stream == StdStream.STDOUT else RE_STDERR
    lines = []
    for line in line_iter(compiler_output, strip=True):
        if match := regex.match(line):
            file_path = expand_path(match[2])
            line = f"{match[1]}{file_path}{line[match.end(2):]}"

        lines.append(line)

    lines.append("")
    return "\r\n".join(lines)


def canonicalize_compile_output(compiler_output: str, stream: StdStream) -> str:
    """Canonicalize the paths in the compiler output."""
    regex = RE_STDOUT if stream == StdStream.STDOUT else RE_STDERR
    lines = []
    for line in line_iter(compiler_output, strip=True):
        if match := regex.match(line):
            orig_path = Path(os.path.normpath(match[2])).absolute()
            # Canonicalize the path
            file_path = canonicalize_path(orig_path)
            line = f"{match[1]}{file_path}{line[match.end(2):]}"

        lines.append(line)

    lines.append("")
    return "\r\n".join(lines)


@functools.cache
def expand_path(path: str) -> Path:
    """Expand a path, replacing placeholders with the actual values."""
    if path.startswith(BASEDIR_REPLACEMENT):
        if BASEDIR_STR:
            return Path(path.replace(
                BASEDIR_REPLACEMENT, str(BASEDIR_STR), 1))
        else:
            raise LogicException(
                f"No CLCACHE_BASEDIR set, but found relative path {path}"
            )
    elif path.startswith(BUILDDIR_REPLACEMENT):
        return Path(path.replace(BUILDDIR_REPLACEMENT, str(BUILDDIR_STR), 1))
    elif CONAN_USER_HOME and path.startswith(CONANDIR_REPLACEMENT):
        return _expand_conan_placeholder(CONAN_USER_HOME, path)
    elif QT_DIR_STR and path.startswith(QTDIR_REPLACEMENT):
        return Path(path.replace(QTDIR_REPLACEMENT, QT_DIR_STR, 1))
    elif LLVM_DIR_STR and path.startswith(LLVM_REPLACEMENT):
        return Path(path.replace(LLVM_REPLACEMENT, LLVM_DIR_STR, 1))
    elif m := RE_ENV.match(path):
        if real_path := _get_env_path(m.group(1)):
            return real_path / path[m.end(0)+1:]
        else:
            raise LogicException(
                f"Unable to resolve environment variable {m.group(1)}"
            )
    else:
        return Path(path)


@functools.cache
def canonicalize_path(path: Path) -> str:
    """Canonicalize a path by applying placeholder replacements."""

    path_str = str(path).lower()

    return (
        _canonicalize_build_dir(path_str)
        or _canonicalize_base_dir(path_str)
        or _canonicalize_conan_dir(path_str)
        or _canonicalize_qt_dir(path_str)
        or _canonicalize_llvm_dir(path_str)
        or _canonicalize_toolchain_dirs(path_str)
        or path_str
    )


def subst_basedir_with_placeholder(src_code: bytes, src_dir: Path) -> bytes:
    """Canonicalize include statements to BASE_DIR in source code

       This is specifically meant to be used for:
       - unity build source files (*_cxx.cxx)
       - Qt moc generated files

       Substitutions performed:
       - #include <BASE_DIR/....>  =>  #include <<BASE_DIR>/....>
       - #include "BASE_DIR/...."  =>  #include "<BASE_DIR>/...."
       - // BASE_DIR/....          =>  // <BASE_DIR>/....
       - the above using relative paths to BASEDIR

       Parameters:
            - src_code: source code as bytes array
            - src_dir: directory of source code file (not the base dir!)

        Returns:
            - canonicalized source code as bytes array
    """
    result: List[bytes] = []

    base_dir_replacement = Path(BASEDIR_REPLACEMENT)

    # iterate over src_code line by line
    line: bytes
    for line in line_iter_b(src_code, strip=True):
        with contextlib.suppress(UnicodeDecodeError, ValueError):
            include_path: Optional[Path] = None
            if m := subst_basedir_with_placeholder.INCLUDE_RE.match(line):
                include_path = Path(m[1].decode())
            elif m := subst_basedir_with_placeholder.COMMENT_RE.match(line):
                include_path = Path(m[1].decode())

            if include_path:
                # test if include path is relative
                if not include_path.is_absolute():
                    include_path = Path(
                        os.path.normpath(src_dir / include_path))

                # check if result in base dir
                if BASEDIR_STR and _is_in_base_dir(include_path):
                    # get relative path to base dir
                    include_path_rel = include_path.relative_to(BASEDIR_STR)

                    # replace with placeholder
                    line = line.replace(
                        m[1], (base_dir_replacement / include_path_rel).as_posix().encode(), 1)
        result.append(line)

    return b'\n'.join(result)


subst_basedir_with_placeholder.INCLUDE_RE = \
    re.compile(br"^\s*#\s*include\s*(?:[\"<])(.*)[\">]", re.IGNORECASE)
subst_basedir_with_placeholder.COMMENT_RE = \
    re.compile(br"^\s*\/\/\s*([^<>|?*\"]+)$", re.IGNORECASE)


@functools.singledispatch
def _is_in_base_dir(path: Path, is_lower=False) -> bool:
    return is_subdir(path, BASEDIR_STR, is_lower) or is_subdir(path, BASEDIR_RESOLVED_STR, is_lower)


@_is_in_base_dir.register(str)
def _(path: str, is_lower=False) -> bool:
    return is_subdir(path, BASEDIR_STR, is_lower) or is_subdir(path, BASEDIR_RESOLVED_STR, is_lower)


@functools.singledispatch
def is_in_build_dir(path: Path, is_lower=False) -> bool:
    return is_subdir(path, BUILDDIR_STR, is_lower) or is_subdir(path, BUILDDIR_RESOLVED_STR, is_lower)


@is_in_build_dir.register(str)
def _(path: str, is_lower=False) -> bool:
    return is_subdir(path, BUILDDIR_STR, is_lower) or is_subdir(path, BUILDDIR_RESOLVED_STR, is_lower)


def set_llvm_dir(compiler_path: Path) -> None:
    re_llvm_dir = re.compile(r"^(.*)(?=\\bin\\clang-cl.exe)", re.IGNORECASE)

    long_path = get_long_path_name(compiler_path)
    if match := re_llvm_dir.match(str(long_path)):
        global LLVM_DIR_STR
        LLVM_DIR_STR = match[1].lower()

    short_path = get_short_path_name(compiler_path)
    if match := re_llvm_dir.match(str(short_path)):
        global LLVM_DIR_SHORT_STR
        LLVM_DIR_SHORT_STR = match[1].lower()


@functools.singledispatch
def is_subdir(path_str: str, prefix: Optional[str], is_lower=False) -> bool:
    """Test if path is a subdirectory of parent."""
    if not is_lower:
        path_str = path_str.lower()
    return bool(prefix and path_str.startswith(prefix.lower()))


@is_subdir.register
def _(path: Path, prefix: Optional[str], is_lower=False) -> bool:
    return is_subdir(str(path), prefix, is_lower)


def subst_with_placeholder(path_str: str, prefix: Optional[str], placeholder: str) -> Optional[str]:
    """Replace path with a placeholder."""
    assert path_str == path_str.lower()

    if prefix:
        prefix_lower = prefix.lower()
        if path_str.startswith(prefix_lower):
            return path_str.replace(prefix, placeholder, 1)

    return None


@functools.cache
def _get_env_path(env: str) -> Optional[Path]:
    '''Get a path from an environment variable, and cache the result.'''
    return resolve(Path(value)) if (value := os.getenv(env)) else None


@functools.cache
def _normalize_dir(dir_path: Path) -> Path:
    '''
    Normalize a directory path, removing trailing slashes.

    This is a workaround for https://bugs.python.org/issue9949
    '''
    result = os.path.normcase(os.path.abspath(os.path.normpath(str(dir_path))))
    if result.endswith(os.path.sep):
        result = result[:-1]
    return Path(result)


@functools.cache
def _get_dir_resolved(path: Path) -> Optional[Path]:
    '''Resolve a path, if it exists.'''
    with contextlib.suppress(Exception):
        resolved = _normalize_dir(resolve(path))
        return resolved if resolved != path else None


def _get_build_dir() -> Path:
    '''
    Get the build directory.

    Get the build directory from the CLCACHE_BUILDDIR environment 
    variable. If it is not set, use the current working directory.
    '''
    if value := os.environ.get("CLCACHE_BUILDDIR"):
        build_dir = Path(value)
        if build_dir.exists():
            return _normalize_dir(build_dir)

    return _normalize_dir(Path.cwd())


def _get_base_dir(build_dir: Path) -> Optional[Path]:
    '''
    Get the base directory.

    Get the base directory from the CLCACHE_BASEDIR environment. 
    If it is not set, determine it from the CMakeCache.txt file.
    '''
    if value := os.environ.get("CLCACHE_BASEDIR"):
        base_dir = Path(value)
        if base_dir.exists():
            return _normalize_dir(base_dir)

    # try loading from CMakeCache.txt inside CLCACHE_BUILDDIR
    cmake_cache_txt = build_dir / "CMakeCache.txt"

    if not cmake_cache_txt.exists():
        return None

    with open(cmake_cache_txt) as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or line.startswith("\n"):
                continue

            name_and_type, value = line.partition("=")[::2]
            name, _ = name_and_type.partition(":")[::2]
            if name == "CMAKE_HOME_DIRECTORY":
                path = Path(value)
                if path.exists():
                    return _normalize_dir(path)


# This is the build dir, where the compiler is executed
BUILDDIR_STR: str = str(_get_build_dir()).lower()

# This is the resolved build dir, where the compiler is executed
BUILDDIR_RESOLVED_STR: Optional[str] = str(
    _get_dir_resolved(Path(BUILDDIR_STR))).lower()

# This is the base dir, where the source code is located
BASEDIR_STR: Optional[str] = str(_get_base_dir(Path(BUILDDIR_STR))).lower()

# This is the resolved base dir, where the source code is located
BASEDIR_RESOLVED_STR: Optional[str] = str(_get_dir_resolved(
    Path(BASEDIR_STR))).lower() if BASEDIR_STR else None


# This is used to find the base dir in the compiler output
BASEDIR_ESC: Optional[str] = BASEDIR_STR.replace(
    "\\", "/") if BASEDIR_STR is not None else None

# This is the build dir, but with forward slashes
BUILDDIR_ESC: str = BUILDDIR_STR.replace("\\", "/")

# Pattern to match the <env:...> placeholder
RE_ENV: re.Pattern[str] = re.compile(r"^<env:([^>]+)>", flags=re.IGNORECASE)

RE_STDOUT: re.Pattern = re.compile(
    r"^(\w+:\s[\s\w]+:\s+)(\S.*)$", re.IGNORECASE)

RE_STDERR: re.Pattern = re.compile(
    r"^(In file included from\s+|)"  # optional prefix
    + r"((?:[A-Z]:|[^\s:]|<[^>]+>)[^:<>|?*\"]*?)"  # path-like
    + r"(?=(?:\d+(?::\d+)?|\(\d+(?:,\d+)?\)|\s\+\d+(?::\d+)?|):)",  # line number
    re.IGNORECASE,
)

# Conan folder path, represented <CONAN_USER_HOME> placeholder
CONAN_USER_HOME: Optional[Path] = None

# Qt folder path, represented <QT_DIR> placeholder
QT_DIR_STR: Optional[str] = None

# LLVM folder path, represented <LLVM> placeholder
LLVM_DIR_STR: Optional[str] = None
LLVM_DIR_SHORT_STR: Optional[str] = None


def _expand_conan_placeholder(conan_user_home: Path, path_str: str) -> Path:
    # This case is more complicated: if the path doesn't exist, we
    # need to inspect the package directory .conan_link files, which
    # will contain the correct path
    # The result of the below will be: ['<CONAN_USER_HOME>', '.conan', 'data',
    # '<package>', '<version>', '<user>', '<channel>', '<package>', '<hash>', ...]
    path_parts = os.path.normpath(path_str).split(os.path.sep)
    link_file = conan_user_home / \
        os.path.sep.join(path_parts[1:9]) / ".conan_link"

    # check if in cache
    if link_file not in _expand_conan_placeholder.cache:
        short_path = None
        if link_file.is_file():
            with open(link_file, "r") as f:
                short_path = Path(os.path.normpath(f.readline()))

        _expand_conan_placeholder.cache[link_file] = short_path

    if short_path := _expand_conan_placeholder.cache[link_file]:
        return short_path / os.path.sep.join(path_parts[9:])
    else:
        return Path(path_str.replace(CONANDIR_REPLACEMENT, str(CONAN_USER_HOME), 1))


_expand_conan_placeholder.cache = {}


def _canonicalize_base_dir(path_str: str) -> Optional[str]:
    """Return the path with the base dir replaced by the placeholder, or None if the path is not in the base dir"""

    if r := subst_with_placeholder(path_str, BASEDIR_STR, BASEDIR_REPLACEMENT):
        return r
    elif r := subst_with_placeholder(path_str, BASEDIR_RESOLVED_STR, BASEDIR_REPLACEMENT):
        return r
    else:
        return None


def _canonicalize_build_dir(path_str: str) -> Optional[str]:
    """Return the path with the build dir replaced by the placeholder, or None if the path is not in the build dir"""

    if r := subst_with_placeholder(path_str, BUILDDIR_STR, BUILDDIR_REPLACEMENT):
        return r
    elif r := subst_with_placeholder(path_str, BUILDDIR_RESOLVED_STR, BUILDDIR_REPLACEMENT):
        return r
    else:
        return None


def _get_conan_user_home_short_re(hint_path: Optional[Path] = None) -> re.Pattern[str]:
    """Get a regex to match the short Conan user home directory, either from the environment or from the hint path"""
    # sourcery skip: assign-if-exp
    if hint_path is None:
        if v := os.environ.get("CONAN_USER_HOME_SHORT"):
            hint_path = Path(v)

    if hint_path is None:
        re_str = rf"[a-z]:\.conan"
    else:
        re_str = re.escape(str(hint_path))

    return re.compile(rf"^({re_str}\\[0-9a-f]+\\1(?=\\))", re.IGNORECASE)


def _get_conan_user_home(hint_path: Optional[Path] = None) -> Path:
    """
    Get the Conan user home directory, either from the environment or from the hint path

        Parameters:
            hint_path (Path): if set, use this path; otherwise use the environment variable CONAN_USER_HOME or the user's home directory
    """
    if hint_path is None:
        if v := os.environ.get("CONAN_USER_HOME"):
            hint_path = Path(v)

    if hint_path is None:
        if v := os.environ.get("USERPROFILE"):
            hint_path = Path(v)

    return hint_path.absolute() if hint_path else Path()


def _get_conan_user_home_re(path: Optional[Path] = None) -> re.Pattern[str]:
    '''
    Get a regex to match the Conan user home directory, either from the environment or from the hint path

        Parameters:
            path (Path): if set, use this path; otherwise use the environment variable CONAN_USER_HOME or the user's home directory

        Returns:
            re.Pattern[str]: a regex to match the Conan user home directory
    '''
    home_re_str = re.escape(str(_get_conan_user_home(path)))
    return re.compile(rf"^{home_re_str}(?=\\\.conan)", re.IGNORECASE)


def _canonicalize_conan_dir(path_str: str) -> Optional[str]:
    '''
    Return the path with the Conan user home directory replaced by the placeholder, or None if the path is not in the Conan user home directory.
    '''
    global CONAN_USER_HOME

    if not _canonicalize_conan_dir.found_venv:
        # try to get the CONAN_USER_HOME from an include file on the compiler command line
        if m := _canonicalize_conan_dir.RE_CONAN_USER_HOME_VENV.match(path_str):
            _canonicalize_conan_dir.found_venv = True
            conan_user_home_from_venv = Path(m.group(1))
            CONAN_USER_HOME = _get_conan_user_home(conan_user_home_from_venv)
            _canonicalize_conan_dir.RE_CONAN_USER_HOME = _get_conan_user_home_re(
                CONAN_USER_HOME)
            _canonicalize_conan_dir.RE_CONAN_USER_SHORT = _get_conan_user_home_short_re(
                CONAN_USER_HOME)

    # Until found a venv, use the default Conan user home
    if CONAN_USER_HOME is None:
        CONAN_USER_HOME = _get_conan_user_home()
        _canonicalize_conan_dir.RE_CONAN_USER_HOME = _get_conan_user_home_re()
        _canonicalize_conan_dir.RE_CONAN_USER_SHORT = _get_conan_user_home_short_re()

    if _canonicalize_conan_dir.RE_CONAN_USER_SHORT is None or \
            _canonicalize_conan_dir.RE_CONAN_USER_HOME is None:
        return None

    # Check for Conan short folder (c:\.conan\) and replace with long form
    if m := _canonicalize_conan_dir.RE_CONAN_USER_SHORT.match(path_str):
        short_path_dir = Path(m.group(1)).parent
        real_path_file = short_path_dir / "real_path.txt"
        if real_path_file.is_file():
            with open(real_path_file, "r") as f:
                # Transform to long form
                real_path = Path(f.readline())
                path_str = str(real_path / path_str[m.end()+1:])

    # Attempt to replace the Conan user home with the placeholder
    mapped_path, cnt = _canonicalize_conan_dir.RE_CONAN_USER_HOME.subn(
        CONANDIR_REPLACEMENT, path_str)
    return mapped_path if cnt > 0 else None


_canonicalize_conan_dir.found_venv = False
_canonicalize_conan_dir.RE_CONAN_USER_HOME = None
_canonicalize_conan_dir.RE_CONAN_USER_SHORT = None
_canonicalize_conan_dir.RE_CONAN_USER_HOME_VENV = re.compile(
    r"^(.*\\gm-venv\\conan_[0-9a-f]+(?=\\))", re.IGNORECASE)


def _canonicalize_toolchain_dirs(path_str: str) -> Optional[str]:
    '''
    Return the path with the toolchain directories replaced by the placeholder, or None if the path is not in the toolchain directory.
    '''

    if _canonicalize_toolchain_dirs.values is None:
        _canonicalize_toolchain_dirs.values = []
        # Order matters!
        ENV_VARS: List[str] = [
            "VCINSTALLDIR",
            "WindowsSdkDir",
            "ExtensionSdkDir",
            "VSINSTALLDIR",
            "CommonProgramFiles",
            "CommonProgramFiles(x86)",
            "ProgramFiles",
            "ProgramFiles(x86)",
            "ProgramData",
            "USERPROFILE",
            "SystemRoot",
            "SystemDrive",
        ]
        for var in ENV_VARS:
            if value := os.environ.get(var):
                long_path = os.path.realpath(os.path.normpath(value)).lower()
                short_path = str(get_short_path_name(Path(long_path))).lower()
                if short_path != long_path:
                    _canonicalize_toolchain_dirs.values.append(
                        (var, long_path, short_path))
                else:
                    _canonicalize_toolchain_dirs.values.append(
                        (var, long_path, None))

    for var, long_path, short_path in _canonicalize_toolchain_dirs.values:
        if short_path and path_str.startswith(short_path + os.path.sep):
            # convert short path to long path
            path_str = os.path.realpath(path_str)
        if path_str.startswith(long_path + os.path.sep):
            return path_str.replace(long_path, f"<env:{var}>", 1)


_canonicalize_toolchain_dirs.values = None


def _canonicalize_qt_dir(path_str: str) -> Optional[str]:
    global QT_DIR_STR

    if QT_DIR_STR is None:
        if m := _canonicalize_qt_dir.RE_QT_DIR.match(path_str):
            QT_DIR_STR = m.group(1)

    if QT_DIR_STR is None:
        return None

    return (
        path_str.replace(QT_DIR_STR, QTDIR_REPLACEMENT, 1)
        if path_str.startswith(QT_DIR_STR)
        else None
    )


_canonicalize_qt_dir.RE_QT_DIR = re.compile(
    rf"^(.*\\Qt)(?=\\\d+\.\d+\.\d+\\)", re.IGNORECASE)


def _canonicalize_llvm_dir(path_str: str) -> Optional[str]:
    if LLVM_DIR_STR and path_str.startswith(LLVM_DIR_STR):
        return path_str.replace(LLVM_DIR_STR, LLVM_REPLACEMENT, 1)
    elif LLVM_DIR_SHORT_STR and path_str.startswith(LLVM_DIR_SHORT_STR):
        return path_str.replace(LLVM_DIR_SHORT_STR, LLVM_REPLACEMENT, 1)
    else:
        return None
