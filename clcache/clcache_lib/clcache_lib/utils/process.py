# Install the library using pip
# pip install pywin32

from subprocess import list2cmdline
import win32api
import win32pipe
import win32process
import win32file
import win32security
import win32event

HANDLE_FLAG_INHERIT = 1
ERROR_BROKEN_PIPE = 109
ERROR_IO_PENDING = 997
ERROR_SUCCESS = 0


def read_output(pipe_handle) -> bytes:
    output: list[bytes] = []
    buffer = win32file.AllocateReadBuffer(65536)
    while True:
        try:
            # Read data from the pipe
            data: bytes
            hr, data = win32file.ReadFile(
                pipe_handle, buffer, None)  # type: ignore
            if hr == ERROR_BROKEN_PIPE:
                break
            if not data:
                break

            output.append(data)
        except Exception as e:
            break

    return b''.join(output)


def create_process_and_capture_output(command: list[str],
                                      env_vars: dict[str, str],
                                      encoding: str) -> tuple[int, str, str]:
    # sourcery skip: extract-method

    # Default initialize all handles
    stdout_read = None
    stdout_write = None
    stderr_read = None
    stderr_write = None
    hp = None
    ht = None

    try:
        # Create pipes for stdout and stderr
        psa = win32security.SECURITY_ATTRIBUTES()
        psa.bInheritHandle = 1

        stdout_read, stdout_write = win32pipe.CreatePipe(psa, 0)
        stderr_read, stderr_write = win32pipe.CreatePipe(psa, 0)

        # Set the pipe handles to non-inheritable
        win32api.SetHandleInformation(stdout_read, HANDLE_FLAG_INHERIT, 0)
        win32api.SetHandleInformation(stderr_read, HANDLE_FLAG_INHERIT, 0)

        # Set up process startup information
        startup_info = win32process.STARTUPINFO()
        startup_info.dwFlags = win32process.STARTF_USESTDHANDLES
        startup_info.hStdOutput = stdout_write
        startup_info.hStdError = stderr_write

        # Create the process
        executable = command[0]
        args = list2cmdline(command[1:])
        
        hp, ht, pid, tid = win32process.CreateProcess(
            executable,             # Application name
            args,                   # Command line
            None,                   # Process security attributes
            None,                   # Thread security attributes
            True,                   # Inherit handles
            0,                      # Creation flags
            env_vars,               # Environment
            None,                   # Current directory
            startup_info            # Startup info
        )

        # Close write handles, as they are not needed anymore
        win32api.CloseHandle(stdout_write)
        win32api.CloseHandle(stderr_write)

        # Read entires stdout and stderr, using AllocateReadBuffer
        std_out = read_output(stdout_read)
        std_err = read_output(stderr_read)

        win32api.CloseHandle(stdout_read)
        win32api.CloseHandle(stderr_read)

        # Wait for the process to finish
        win32event.WaitForSingleObject(hp, win32event.INFINITE)

        # Get the process exit code
        exit_code = win32process.GetExitCodeProcess(hp)

        # Close handles
        win32api.CloseHandle(hp)
        win32api.CloseHandle(ht)

        # Return the process exit code and the captured output
        return exit_code, std_out.decode(encoding), std_err.decode(encoding)

    except Exception as e:
        # Close all handles
        if stdout_read is not None:
            win32api.CloseHandle(stdout_read)
        if stdout_write is not None:
            win32api.CloseHandle(stdout_write)
        if stderr_read is not None:
            win32api.CloseHandle(stderr_read)
        if stderr_write is not None:
            win32api.CloseHandle(stderr_write)
        if hp is not None:
            win32api.CloseHandle(hp)
        if ht is not None:
            win32api.CloseHandle(ht)

        raise e
