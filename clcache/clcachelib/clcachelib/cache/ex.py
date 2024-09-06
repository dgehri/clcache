

class IncludeNotFoundException(Exception):
    pass


class CompilerFailedException(Exception):
    def __init__(self, exit_code: int, msg_err: str, msg_out: str = ""):
        super().__init__(msg_err)
        self.exit_code = exit_code
        self.msg_out = msg_out
        self.msg_err = msg_err

    def get_compiler_result(self) -> tuple[int, str, str]:
        return self.exit_code, self.msg_err, self.msg_out


class LogicException(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return repr(self.message)


class ProfilerError(Exception):
    def __init__(self, returnCode):
        self.returnCode = returnCode
