clcache.py - a compiler cache for Microsoft Visual Studio
---------------------------------------------------------

clcache.py is a little Python script which attempts to avoid unnecessary
recompilation by reusing previously cached object files if possible. It
is meant to be called instead of the original 'cl.exe' executable. The
script analyses the command line to decide whether source code is
to be compiled. If so, a cache will be queried for a previously stored
object file.

If the script is called in an unsupported way (e.g. if the compiler is
called for linking), the script will simply relay the invocation to the real
'cl.exe' program.

image:https://ci.appveyor.com/api/projects/status/sf98y2686r00q6ga/branch/master?svg=true[Build status, link="https://ci.appveyor.com/project/frerich/clcache"]
image:https://codecov.io/gh/frerich/clcache/branch/master/graph/badge.svg[Code coverage, link="https://codecov.io/gh/frerich/clcache"]

Installation
~~~~~~~~~~~~

Please see the https://github.com/frerich/clcache/wiki[Wiki] for instructions
on how to https://github.com/frerich/clcache/wiki/Installation[install] clcache
and different approaches on how to
https://github.com/frerich/clcache/wiki/Integration[integrate] it into a build
system.

Options
~~~~~~~

--help::
    Print usage information
-s::
    Print some statistics about the cache (cache hits, cache misses, cache
    size etc.)
-c::
    Clean the cache: trim the cache size to 90% of its maximum by removing
    the oldest objects.
-C::
    Clear the cache: remove all cached objects, but keep the cache statistics
    (hits, misses, etc.).
-z::
    Reset the cache statistics, i.e. number of cache hits, cache misses etc..
    Doesn't actually clear the cache, so the number of cached objects and the
    cache size will remain unchanged.
-M <size>::
    Sets the maximum size of the cache in bytes.
    The default value is 1073741824 (1 GiB).

compiler::
    It is, optionally, possible to specify the full path to the compiler as the
    first argument on the command line, in the style of ccache, instead of using
    the CLCACHE_CL environment variable or searching the path for cl.exe   

Environment Variables
~~~~~~~~~~~~~~~~~~~~~

CLCACHE_DIR::
    If set, points to the directory within which all the cached object files
    should be stored. This defaults to `%HOME%\clcache`
CLCACHE_CL::
    Can be set to the actual 'cl.exe' executable to use. If this variable is
    not set, the 'clcache.py' script will scan the directories listed in the
    +PATH+ environment variable for 'cl.exe'. In case this is just a file name
    (as opposed to an absolute path), 'clcache.py' will scan the directories
    mentioned by the `%PATH%` environment variable to compute the absolute
    path.
CLCACHE_LOG::
    If this variable is set, a bit of diagnostic information is printed which
    can help with debugging cache problems.
CLCACHE_DISABLE::
    Setting this variable will disable 'clcache.py' completely. The script will
    relay all calls to the real compiler.
CLCACHE_HARDLINK::
    If this variable is set, cached object files won't be copied to their
    final location. Instead, hard links pointing to the cached object files
    will be created. This is more efficient (faster, and uses less disk space)
    but doesn't work if the cache directory is on a different drive than the
    build directory.
CLCACHE_COMPRESS::
    If true, clcache will compress object files it puts in the cache. If the cache
    was filled without compression it can't be used with compression and vice versa
    (i.e. you have to clear the cache when changing this setting). The default is false.
CLCACHE_COMPRESSLEVEL::
    This setting determines the level at which clcache will compress object files.
    It only has effect if compression is enabled. The value defaults to 6, and
    must be no lower than 1 (fastest, worst compression) and no higher than 9
    (slowest, best compression).
CLCACHE_NODIRECT::
    Disable direct mode. If this variable is set, clcache will always run
    preprocessor on source file and will hash preprocessor output to get cache
    key. Use this if you experience problems with direct mode or if you need
    built-in macroses like \__TIME__ to work correctly.
CLCACHE_BASEDIR::
    Has effect only when direct mode is on. Set this to path to root directory
    of your project. This allows clcache to cache relative paths, so if you
    move your project to different directory, clcache will produce cache hits as
    before.
CLCACHE_BUILDDIR::
    Set this to path to your build directory. This allows clcache to cache relative
    paths, so if you move your project to different directory, clcache will 
    produce cache hits as before. If not specified, then the current working directory
    is used.
CLCACHE_OBJECT_CACHE_TIMEOUT_MS::
    Overrides the default ObjectCacheLock timeout (Default is 10 * 1000 ms).
    The ObjectCacheLock is used to give exclusive access to the cache, which is
    used by the clcache script. You may override this variable if you are
    getting ObjectCacheLockExceptions with return code 258 (which is the
    WAIT_TIMEOUT return code).
CLCACHE_PROFILE::
    If this variable is set, clcache will generate profiling information about
    how the runtime is spent in the clcache code. For each invocation, clcache
    will generate a file with a name similiar to 'clcache-<hashsum>.prof'. You
    can aggregate these files and generate a report by running the
    'showprofilereport.py' script.
CLCACHE_SERVER::
    Setting this environment variable will make clcache use (and expect) a
    running `clcachesrv.py` script which takes care of caching file hashes.
    This greatly improves performance of cache hits, but only has an effect in
    direct mode (i.e. when `CLCACHE_NODIRECT` is not set).
CLCACHE_MEMCACHED::
    This variable can be used to make clcache use a
    memcached[https://memcached.org/] backend for saving and restoring cached
    data. The variable is assumed to hold the host and port information of the
    memcached server, e.g. `127.0.0.1:11211`.


Known limitations
~~~~~~~~~~~~~~~~~

* https://msdn.microsoft.com/en-us/library/kezkeayy.aspx[+INCLUDE+ and +LIBPATH+]
  environment variables are not supported.

How clcache works
~~~~~~~~~~~~~~~~~

clcache.py was designed to intercept calls to the actual cl.exe compiler
binary. Once an invocation has been intercepted, the command line is analyzed for
whether it is a command line which just compiles a single source file into an
object file. This means that all of the following requirements on the command
line must be true:

* The +/link+ switch must not be present
* The +/c+ switch must be present
* The +/Zi+ switch must not be present (+/Z7+ is okay though)

If multiple source files are given on the command line, clcache.py wil invoke
itself multiple times while respecting an optional +/MP+ switch.

If all the above requirements are met, clcache forwards the call to the
preprocessor by replacing +/c+ with +/EP+ in the command line and then
invoking it. This will cause the complete preprocessed source code to be
printed. clcache then generates a hash sum out of

* The complete preprocessed source code
* The `normalized' command line
* The file size of the compiler binary
* The modification time of the compiler binary

The `normalized' command line is the given command line minus all switches
which either don't influence the generated object file (such as +/Fo+) or
which have already been covered otherwise. For instance, all switches which
merely influence the preprocessor can be skipped since their effect is already
implicitly contained in the preprocessed source code.

Once the hash sum is computed, it is used as a key (actually, a directory
name) in the cache (which is a directory itself). If the cache entry exists
already, it is supposed to contain a file with the stdout output of the
compiler as well as the previously generated object file. clcache will
copy the previously generated object file to the designated output path and
then print the contents of the stdout text file. That way, the script
behaves as if the actual compiler was invoked.

If the hash sum is not yet used in the cache, clcache will forward the
invocation to the actual compiler. Once the real compiler successfully
finished its work, the generated object file (as well as the output printed
by the compiler) is copied to the cache.

Caveats
~~~~~~~
For known caveats, please see the
https://github.com/frerich/clcache/wiki/Caveats[Caveats wiki page].

License Terms
~~~~~~~~~~~~~
The source code of this project is - unless explicitly noted otherwise in the
respective files - subject to the
https://opensource.org/licenses/BSD-3-Clause[BSD 3-Clause License].

Credits
~~~~~~~
clcache.py was written by mailto:raabe@froglogic.com[Frerich Raabe] with a lot
of help by mailto:vchigrin@yandex-team.ru[Slava Chigrin], Simon Warta, Tim
Blechmann, Tilo Wiedera and other contributors.

This program was heavily inspired by http://ccache.samba.org[ccache], a
compiler cache for the http://gcc.gnu.org[GNU Compiler Collection].
