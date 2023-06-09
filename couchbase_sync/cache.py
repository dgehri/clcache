
from pathlib import Path
from typing import BinaryIO, Callable, NamedTuple


class CompilerArtifacts(NamedTuple):
    '''
    Represents a set of artifacts produced by a compiler invocation

        - obj_file_path: path to the object file
        - stdout: stdout of the compiler
        - stderr: stderr of the compiler
    '''
    payload_path: Path
    stdout: str
    stderr: str
    copy_filter: Callable[[BinaryIO, BinaryIO], None] | None = None


class ManifestEntry(NamedTuple):
    '''
    An entry in a manifest file

        - includeFiles: list of paths to include files, which this source file uses
        - includesContentHash: hash of the contents of the include_files
        - objectHash: hash calculated from includeContentHash and the manifest hash
    '''
    includeFiles: list[str]
    includesContentHash: str
    objectHash: str

    def __hash__(self):
        '''
        Returns the hash

        The includesContentHash is a function of the includeFiles, 
        while the objectHash is a function of the manifest hash and the 
        includesContentHash. Therefore, for a given manifest file, the 
        includesContentHash uniquely identifies the entry.
        '''
        return hash(self.includesContentHash)


class Manifest:
    '''Represents a manifest file'''

    def __init__(self, entries: list[ManifestEntry] | None = None):
        if entries is None:
            entries = []
        self._entries: list[ManifestEntry] = entries.copy()

    def entries(self) -> list[ManifestEntry]:
        return self._entries

    def add_entry(self, entry: ManifestEntry):
        """Adds entry at the top of the entries"""
        # Remove existing entry with the same includeHash
        self._entries = [
            e for e in self._entries if e.includesContentHash != entry.includesContentHash]
        self._entries.insert(0, entry)

    def touch_entry(self, obj_hash: str):
        """Moves entry in entry_index position to the top of entries()"""
        entry_index = next(
            (i for i, e in enumerate(self.entries())
             if e.objectHash == obj_hash), 0
        )
        self._entries.insert(0, self._entries.pop(entry_index))