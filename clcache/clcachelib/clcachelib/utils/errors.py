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


class CalledForJsonOutputError(AnalysisError):
    pass


class CalledWithoutOutputFile(AnalysisError):
    pass


class InvalidArgumentError(AnalysisError):
    pass
