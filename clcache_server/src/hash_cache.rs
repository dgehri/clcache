pub(crate) use anyhow::{Context, Result};

use log::{error, trace};

use std::{
    fs::File,
    io::BufRead,
    path::{Path, PathBuf},
    sync::Arc,
    time::SystemTime,
};

use dashmap::DashMap;

struct HashEntry {
    hash: String,
    last_modified: SystemTime,
}

/// Maps file paths to hashes and last modified times.
type FileHashDict = DashMap<PathBuf, HashEntry>;

/// A cache of file hashes.
#[derive(Clone)]
pub struct HashCache {
    /// Maps watched directories to a map of file names to hashes.
    cache: Arc<FileHashDict>,
}

impl HashCache {
    pub fn new() -> Self {
        HashCache {
            cache: Arc::new(FileHashDict::new()),
        }
    }

    /// Returns the hash of the file at the given path. If the file is not in the cache, it is
    /// calculated and added to the cache.
    ///
    /// Parameters:
    ///     path: The path to the file.
    ///
    /// Returns:
    ///    The hash of the file.
    pub async fn get_file_hash(&self, path: &Path) -> Result<String> {
        // fully resolve the path (symlinks, mapped drives, etc.)
        let resolved_path = path
            .canonicalize()
            .with_context(|| format!("Failed to resolve path '{}'", path.display()))?;

        // look up path in cache, calculate hash if not found and add to cache
        let hash = match self.cache.get_mut(&resolved_path) {
            Some(mut entry) => {
                // check if file has been modified
                let metadata = match std::fs::metadata(&resolved_path) {
                    Ok(metadata) => metadata,
                    Err(e) => {
                        error!(
                            "Failed to get metadata for file '{}': {}",
                            path.display(),
                            e
                        );
                        return Err(e.into());
                    }
                };

                let modified = match metadata.modified() {
                    Ok(modified) => modified,
                    Err(e) => {
                        error!(
                            "Failed to get modification time for file '{}': {}",
                            path.display(),
                            e
                        );
                        return Err(e.into());
                    }
                };

                if modified != entry.last_modified {
                    // file has been modified, recalculate hash
                    trace!(
                        "File '{}' has been modified, recalculating hash",
                        path.display()
                    );

                    let hash = HashCache::calculate_hash(&resolved_path)?;

                    // update cache
                    entry.hash = hash.clone();
                    entry.last_modified = modified;

                    hash
                } else {
                    // file has not been modified, return cached hash
                    entry.value().hash.clone()
                }
            }
            None => {
                // file not in cache, calculate hash
                trace!("File '{}' not in cache, calculating hash", path.display());

                let hash = HashCache::calculate_hash(&resolved_path)?;

                // add to cache
                self.cache.insert(
                    resolved_path.clone(),
                    HashEntry {
                        hash: hash.clone(),
                        last_modified: SystemTime::now(),
                    },
                );

                hash
            }
        };

        Ok(hash)
    }

    // same as above, but multithreaded. The returned hashes are in the same order as the paths.
    pub async fn get_file_hashes(&self, paths: &[PathBuf]) -> Result<Vec<String>> {
        let mut hashes = Vec::with_capacity(paths.len());

        let mut futures = Vec::with_capacity(paths.len());

        for path in paths {
            let path = path.clone();
            let self_ = self.clone();
            futures.push(tokio::spawn(
                async move { self_.get_file_hash(&path).await },
            ));
        }

        for future in futures {
            hashes.push(future.await??);
        }

        Ok(hashes)
    }

    pub async fn clear(&self) {
        // clear the cache
        self.cache.clear();
    }

    fn calculate_hash(path: &Path) -> Result<String> {
        let f = File::open(path)?;

        // Find the length of the file
        let len = f.metadata()?.len();

        // Decide on a reasonable buffer size (1MB in this case, fastest will depend on hardware)
        let buf_len = len.min(1_000_000) as usize;
        let mut buf = std::io::BufReader::with_capacity(buf_len, f);
        let mut context = md5::Context::new();
        loop {
            // Get a chunk of the file
            let part = buf.fill_buf()?;

            // If that chunk was empty, the reader has reached EOF
            if part.is_empty() {
                break;
            }
            // Add chunk to the md5
            context.consume(part);

            // Tell the buffer that the chunk is consumed
            let part_len = part.len();
            buf.consume(part_len);
        }
        let digest = context.compute();

        Ok(format!("{:x}", digest))
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn calculate_hash_1() {
        let hash = super::HashCache::calculate_hash(&std::path::PathBuf::from(
            "tests/res/1/qjsonrpcservice.h",
        ))
        .unwrap();
        assert_eq!(hash, "1e69f8ad0d5e16cad26ab3bb454cf841");
    }

    #[test]
    fn calculate_hash_2() {
        let result = super::HashCache::calculate_hash(&std::path::PathBuf::from(
            "tests/res/2/qjsonrpcservice.h",
        ));

        assert!(result.is_err());
    }
}
