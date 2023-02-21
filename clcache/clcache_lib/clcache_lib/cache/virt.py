import contextlib
import os
from pathlib import Path
import re

from ..config import CACHE_COMPILER_OUTPUT_STORAGE_CODEC
from ..utils import normalize_dir, get_actual_filename
from .ex import LogicException

# String, by which BASE_DIR will be replaced in paths, stored in manifests.
BASEDIR_REPLACEMENT = "<BASE_DIR>"
BUILDDIR_REPLACEMENT = "<BUILD_DIR>"
CONANDIR_REPLACEMENT = "<CONAN_USER_HOME>"


def get_build_dir() -> str:
    result = os.environ.get("CLCACHE_BUILDDIR")
    if result is None or not os.path.exists(result):
        result = os.getcwd()

    return normalize_dir(result)  # type: ignore


BUILDDIR = get_build_dir()

BUILDDIR_RESOLVED = None
if BUILDDIR:
    with contextlib.suppress(Exception):
        resolved = normalize_dir(Path(BUILDDIR).resolve())
        if resolved != BUILDDIR:
            BUILDDIR_RESOLVED = resolved
BASEDIR = normalize_dir(os.environ.get("CLCACHE_BASEDIR"))

if BASEDIR is None or not os.path.exists(BASEDIR):
    # try loading from CMakeCache.txt inside CLCACHE_BUILDDIR
    cmakeCache = f"{BUILDDIR}/CMakeCache.txt"

    if os.path.exists(cmakeCache):
        with open(cmakeCache) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or line.startswith("\n"):
                    continue

                nameAndType, value = line.partition("=")[::2]
                name, varType = nameAndType.partition(":")[::2]
                if name == "CMAKE_HOME_DIRECTORY":
                    if os.path.exists(value):
                        BASEDIR = normalize_dir(value)
                    break

BASEDIR_RESOLVED = None
if BASEDIR:
    with contextlib.suppress(Exception):
        resolved = normalize_dir(Path(BASEDIR).resolve())
        if resolved != BASEDIR:
            BASEDIR_RESOLVED = resolved

# Need to substitute BASEDIR and BUILDDIR in the following
# - "^<some text w/o colons> <path>[^:]*:"
# - "^<path>[^:]*:"


def get_basedir_diag_regex(path):
    regex = rf'^([^:?*"<>|\\\/]+\s)?(?:{path})([^:?*"<>|]+:)'
    return re.compile(regex, re.IGNORECASE | re.MULTILINE)


BASEDIR_DIAG_RE = (
    get_basedir_diag_regex(re.sub(r"[\\\/]", r"[\\\/]", BASEDIR))
    if BASEDIR is not None
    else None
)
BASEDIR_DIAG_RE_INV = (
    get_basedir_diag_regex(re.escape(BASEDIR_REPLACEMENT))
    if BASEDIR is not None
    else None
)
BASEDIR_ESC = BASEDIR.replace("\\", "/") if BASEDIR is not None else None


def get_builddir_diag_regex(path):
    regex = rf'^([^:?*"<>|\\\/]+\s)?(?:{path})([^:?*"<>|]+:)'
    return re.compile(regex, re.IGNORECASE | re.MULTILINE)


BUILDDIR_DIAG_RE = get_builddir_diag_regex(
    re.sub(r"[\\\/]", r"[\\\/]", BUILDDIR))
BUILDDIR_DIAG_RE_INV = get_builddir_diag_regex(re.escape(BUILDDIR_REPLACEMENT))
BUILDDIR_ESC = BUILDDIR.replace("\\", "/")


def get_cached_compiler_console_output(path, translatePaths=False):
    try:
        with open(path, "rb") as f:
            output = f.read().decode(CACHE_COMPILER_OUTPUT_STORAGE_CODEC)
            if translatePaths:
                output = BUILDDIR_DIAG_RE_INV.sub(
                    rf"\1{BUILDDIR_ESC}\2", output)
                if BASEDIR_DIAG_RE_INV is not None:
                    output = BASEDIR_DIAG_RE_INV.sub(
                        rf"\1{BASEDIR_ESC}\2", output)
            return output
    except IOError:
        return ""


def set_cached_compiler_console_output(path, output, translatePaths=False):
    if translatePaths:
        output = BUILDDIR_DIAG_RE.sub(rf"\1{BUILDDIR_REPLACEMENT}\2", output)

        if BASEDIR_DIAG_RE is not None:
            output = BASEDIR_DIAG_RE.sub(rf"\1{BASEDIR_REPLACEMENT}\2", output)

    with open(path, "wb") as f:
        f.write(output.encode(CACHE_COMPILER_OUTPUT_STORAGE_CODEC))


RE_ENV = re.compile(r"^<env:([^>]+)>", flags=re.IGNORECASE)


def expandDirPlaceholder(path):
    if path.startswith(BASEDIR_REPLACEMENT):
        if not BASEDIR:
            raise LogicException(
                f"No CLCACHE_BASEDIR set, but found relative path {path}"
            )
        return path.replace(BASEDIR_REPLACEMENT, BASEDIR, 1)
    elif path.startswith(BUILDDIR_REPLACEMENT):
        return path.replace(BUILDDIR_REPLACEMENT, BUILDDIR, 1)
    elif path.startswith(CONANDIR_REPLACEMENT) and CONAN_USER_HOME:
        # This case is more complicated: if the path doesn't exist, we
        # need to inspect the package directory .conan_link files, which
        # will contain the correct path
        # The result of the below will be: ['<CONAN_USER_HOME>', '.conan', 'data', '<package>', '<version>', '<user>', '<channel>', '<package>', '<hash>', ...]
        path_parts = os.path.normpath(path).split(os.path.sep)
        link_file = os.path.join(
            CONAN_USER_HOME, os.path.sep.join(path_parts[1:9]), ".conan_link"
        )
        if os.path.isfile(link_file):
            with open(link_file, "r") as f:
                short_path = os.path.normpath(f.readline())
                return os.path.join(short_path, os.path.sep.join(path_parts[9:]))
        else:
            return path.replace(CONANDIR_REPLACEMENT, CONAN_USER_HOME, 1)
    elif path.startswith(QTDIR_REPLACEMENT) and QT_DIR:
        return path.replace(QTDIR_REPLACEMENT, QT_DIR, 1)
    else:
        m = RE_ENV.match(path)
        if m is not None:
            env_val = os.environ.get(m.group(1))
            if env_val is not None:
                real_path = os.path.realpath(
                    os.path.normcase(os.path.normpath(env_val))
                )
                return RE_ENV.sub(real_path.replace("\\", "\\\\"), path)
        return path


def collapseBaseDirToPlaceholder(path):
    if BASEDIR and path.startswith(BASEDIR):
        return (path.replace(BASEDIR, BASEDIR_REPLACEMENT, 1), True)
    elif BASEDIR_RESOLVED and path.startswith(BASEDIR_RESOLVED):
        return (path.replace(BASEDIR_RESOLVED, BASEDIR_REPLACEMENT, 1), True)
    else:
        return (path, False)


def collapseBuildDirToPlaceholder(path):
    if path.startswith(BUILDDIR):
        return (path.replace(BUILDDIR, BUILDDIR_REPLACEMENT, 1), True)
    elif BUILDDIR_RESOLVED and path.startswith(BUILDDIR_RESOLVED):
        return (path.replace(BUILDDIR_RESOLVED, BUILDDIR_REPLACEMENT, 1), True)
    else:
        return (path, False)


def get_conan_user_home_short_re(path=None):
    if path is None:
        path = os.environ.get("CONAN_USER_HOME_SHORT")
    if path is None:
        path = r"[a-z]:[\\\/].conan"
    else:
        path = path.replace('[', r'\[')
        path = re.sub(r"[\\\/](?!\[)", r"[\\\/]", path)

    return re.compile(rf"^({path}[\\\/][0-9a-f]+[\\\/]1(?=[\\\/]))", re.IGNORECASE)


def get_conan_user_home(path=None):
    if path is None:
        path = os.environ.get("CONAN_USER_HOME")

    if path is None:
        path = rf"{os.environ.get('USERPROFILE')}"

    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def get_conan_user_home_re(path=None):
    if path is not None:
        return re.compile("^" + re.sub(r"[\\\/]", r"[\\\/]", get_conan_user_home(path)), re.IGNORECASE)
    else:
        return re.compile("^" + re.sub(r"[\\\/]", r"[\\\/]", get_conan_user_home(path)) + r"(?=[\\\/]\.conan)", re.IGNORECASE)


CONAN_USER_HOME = None
RE_CONAN_USER_HOME = None
RE_CONAN_USER_SHORT = None
CONAN_USER_HOME_FROM_VENV = None
RE_CONAN_USER_HOME_VENV = re.compile(
    r"^(.*[\\\/]gm-venv[\\\/]conan_[0-9a-f]+(?=[\\\/]))", re.IGNORECASE)


def canonicalizeConanPath(conan_path: str):
    # Check if the conan_path matches the CONAN_USER_HOME from a venv
    global CONAN_USER_HOME_FROM_VENV, CONAN_USER_HOME, RE_CONAN_USER_HOME, RE_CONAN_USER_SHORT
    if CONAN_USER_HOME_FROM_VENV is None:
        m = RE_CONAN_USER_HOME_VENV.match(conan_path)
        if m is not None:
            CONAN_USER_HOME_FROM_VENV = m.group(1)
            CONAN_USER_HOME = get_conan_user_home(CONAN_USER_HOME_FROM_VENV)
            RE_CONAN_USER_HOME = get_conan_user_home_re(CONAN_USER_HOME)
            RE_CONAN_USER_SHORT = get_conan_user_home_short_re(CONAN_USER_HOME)

    if CONAN_USER_HOME is None:
        CONAN_USER_HOME = get_conan_user_home()
        RE_CONAN_USER_HOME = get_conan_user_home_re()
        RE_CONAN_USER_SHORT = get_conan_user_home_short_re()

    # Check for Conan short folder (c:\.conan\)
    m = RE_CONAN_USER_SHORT.match(conan_path)
    if m is not None:
        real_path_file = os.path.join(
            os.path.dirname(m.group(1)), "real_path.txt")
        if os.path.isfile(real_path_file):
            with open(real_path_file, "r") as f:
                # Transform to long form
                real_path = f.readline()
                conan_path = RE_CONAN_USER_SHORT.sub(
                    real_path.replace("\\", "\\\\"), conan_path
                )

    # Otherwise check for long folder
    result = RE_CONAN_USER_HOME.sub(CONANDIR_REPLACEMENT, conan_path)
    return (result, result is not conan_path)


# Canonicalize Qt folder
QT_DIR = None
QTDIR_REPLACEMENT = "<QT_DIR>"
RE_QT_DIR = re.compile(
    rf"^(.*[\\\/]Qt[\\\/]\d+\.\d+\.\d+(?=[\\\/]))", re.IGNORECASE)


def canonicalizeQtPath(str: str):
    global QT_DIR
    if QT_DIR is None:
        m = RE_QT_DIR.match(str)
        if m is not None:
            QT_DIR = m.group(1)

    result = RE_QT_DIR.sub(QTDIR_REPLACEMENT, str)
    return result, result != str


def collapseDirToPlaceholder(path):
    (path, done) = collapseBuildDirToPlaceholder(path)
    if done:
        return path

    (path, done) = collapseBaseDirToPlaceholder(path)
    if done:
        return path

    (path, done) = canonicalizeConanPath(path)
    if done:
        return path

    (path, done) = canonicalizeQtPath(path)
    if done:
        return path

    (path, done) = canonicalizeEnvPath(path)
    return path


RE_STDOUT = re.compile(r"^(\w+:\s[\s\w]+:\s+)(\S.*)$", re.IGNORECASE)
RE_STDERR = re.compile(
    r"^(In file included from\s+)?([A-Z]:.*?|[^\s:][^:]*?)(?=(?:\d+(?::\d+)?|\(\d+(?:,\d+)?\)|\s\+\d+(?::\d+)?|):)",
    re.IGNORECASE,
)


def expandDirPlaceholderInCompileOutput(compilerOutput: str, re: re.Pattern):
    lines = []
    for line in compilerOutput.splitlines(True):
        line = line.rstrip("\r\n")
        match = re.match(line)
        if match is not None:
            file_path = get_actual_filename(expandDirPlaceholder(match[2]))
            line = re.sub(r"\1" + file_path.replace("\\", "\\\\"), line)
        lines.append(line)

    lines.append("")
    return "\r\n".join(lines)


def collapseDirPlaceholderInCompileOutput(compilerOutput: str, re: re.Pattern):
    lines = []
    for line in compilerOutput.splitlines(True):
        line = line.rstrip("\r\n")
        match = re.match(line)
        if match is not None:
            file_path = os.path.normcase(
                os.path.abspath(os.path.normpath(match[2])))
            file_path = collapseDirToPlaceholder(file_path)
            line = re.sub(r"\1" + file_path.replace("\\", "\\\\"), line)
        lines.append(line)

    lines.append("")
    return "\r\n".join(lines)


def canonicalizeEnvPath(str: str):
    real_path = os.path.realpath(str)
    # Order matters!
    env_vars = [
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
    for e in env_vars:
        env_var = os.environ.get(e)
        if env_var is not None:
            env_path = os.path.realpath(
                os.path.normpath(os.path.normcase(env_var)))
            if real_path.startswith(env_path + os.path.sep):
                return (real_path.replace(env_path, f"<env:{e}>"), True)
    return (str, False)


# Regex for replacing the following with '?':
#
# #include <BASE_DIR/....>  =>  #include <<BASE_DIR>/....>
# #include "BASE_DIR/...."  =>  #include "<BASE_DIR>/...."
# // BASE_DIR/....          =>  // ?/....


def getBaseDirSourceRegex():
    if BASEDIR is None:
        return None

    baseDirRegex = re.sub(r"[\\\/]", r"[\\\/]", BASEDIR)

    try:
        # The following may fail if BUILDDIR is on a different drive than BASEDIR
        buildPathRelRegex = re.sub(
            r"[\\\/]", r"[\\\/]", os.path.relpath(BUILDDIR, BASEDIR)
        )
        fullRegex = rf'((?:^|\n)\s*(?:#\s*include\s+["<]|\/\/\s*)){baseDirRegex}(?![\\/]{buildPathRelRegex})'
        return re.compile(fullRegex.encode(), re.IGNORECASE)
    except Exception:
        fullRegex = rf'((?:^|\n)\s*(?:#\s*include\s+["<]|\/\/\s*)){baseDirRegex}'
        return re.compile(fullRegex.encode(), re.IGNORECASE)


BASE_DIR_SRC_RE = getBaseDirSourceRegex()


def substituteIncludeBaseDirPlaceholder(str: str):
    if BASE_DIR_SRC_RE is None:
        return str
    else:
        # Replace #include "CLCACHE_BASEDIR" by ? in source code
        return BASE_DIR_SRC_RE.sub(rb"\1" + BASEDIR_REPLACEMENT.encode(), str)
