from pathlib import Path

from ..cache.file_cache import (CompilerArtifactsRepository, ManifestEntry,
                                ManifestRepository)
from ..cache.hash import get_file_hashes
from ..cache.virt import canonicalize_path


def create_manifest_entry(manifest_hash: str, include_paths: list[Path]) -> ManifestEntry:
    """
    Create a manifest entry for the given manifest hash and include paths.
    """

    sorted_include_paths = sorted(set(include_paths))
    include_hashes = get_file_hashes(sorted_include_paths)

    safe_includes = [canonicalize_path(path) for path in sorted_include_paths]
    content_hash = ManifestRepository.get_includes_content_hash_for_hashes(
        include_hashes
    )
    objectHash = CompilerArtifactsRepository.compute_key(
        manifest_hash, content_hash
    )

    return ManifestEntry(safe_includes, content_hash, objectHash)