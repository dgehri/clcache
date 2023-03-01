from typing import Tuple


class IncludeNotFoundException(Exception):
    pass


class CacheLockException(Exception):
    pass


class CompilerFailedException(Exception):
    def __init__(self, exitCode: int, msgErr: str, msgOut: str = ""):
        super(CompilerFailedException, self).__init__(msgErr)
        self.exitCode = exitCode
        self.msgOut = msgOut
        self.msgErr = msgErr

    def getReturnTuple(self) -> Tuple[int, str, str]:
        return self.exitCode, self.msgErr, self.msgOut


class LogicException(Exception):
    def __init__(self, message):
        super(LogicException, self).__init__(message)
        self.message = message

    def __str__(self):
        return repr(self.message)


class ProfilerError(Exception):
    def __init__(self, returnCode):
        self.returnCode = returnCode
