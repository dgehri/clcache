
from couchbase.transcoder import *  # type: ignore


class RawBinaryTranscoderEx(Transcoder):
    def encode_value(
        self, value: Union[bytes,bytearray]
    ) -> tuple[bytes, int]:

        if not isinstance(value, (bytes, (bytearray, memoryview))):
            raise ValueFormatException(
                "Only binary data supported by RawBinaryTranscoder"
            )
        if isinstance(value, (bytearray, memoryview)):
            value = bytes(value)
        return value, FMT_BYTES

    def decode_value(
        self,
        value: bytes,
        flags: int
    ) -> bytes:

        fmt = get_decode_format(flags)

        if fmt == FMT_BYTES:
            if isinstance(value, (bytearray, memoryview)):
                value = bytes(value)
            return value
        elif fmt == FMT_UTF8:
            raise ValueFormatException(
                "String format type not supported by RawBinaryTranscoder"
            )
        elif fmt == FMT_JSON:
            raise ValueFormatException(
                "JSON format type not supported by RawBinaryTranscoder"
            )
        else:
            raise InvalidArgumentException("Unexpected flags value.")
