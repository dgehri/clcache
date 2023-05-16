pub(crate) use anyhow::{Context, Result};

use log::{debug, error, trace};
use notify::{Config, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};

use std::{
    collections::{HashMap, HashSet},
    fs::File,
    io::BufRead,
    path::{Path, PathBuf},
    sync::Arc,
    time::SystemTime,
};

use tokio::{
    runtime::Runtime,
    sync::{mpsc, Mutex},
};

use dashmap::DashMap;

struct HashEntry {
    hash: String,
    last_modified: SystemTime,
}

/// Maps file names to hashes.
type FileHashDict = HashMap<String, HashEntry>;

/// Maps directories to a map of file names to hashes.
type DirectoryToFileHashDict = DashMap<PathBuf, FileHashDict>;

/// The behavior of the file system watcher.
#[derive(Debug, Clone, Copy)]
pub enum WatchBehavior {
    /// Watch file and remove from cache if it changes
    MonitorForChanges,

    /// Do not watch file
    DoNotMonitor,
}

struct DirWatcher {
    watcher: RecommendedWatcher,
    watched_dirs: HashSet<PathBuf>,
}

/// A cache of file hashes.
pub struct HashCache {
    /// Maps watched directories to a map of file names to hashes.
    cache: Arc<DirectoryToFileHashDict>,

    // file system watcher
    dir_watcher: Option<Arc<Mutex<DirWatcher>>>,
}

impl HashCache {
    pub fn new(watch_behavior: WatchBehavior) -> Self {
        let cache = Arc::new(DirectoryToFileHashDict::new());

        let dir_watcher = match watch_behavior {
            WatchBehavior::MonitorForChanges => {
                let (tx, rx) = mpsc::channel(1);

                let dir_watcher = Arc::new(Mutex::new(DirWatcher {
                    watcher: RecommendedWatcher::new(
                        move |res| {
                            let rt = Runtime::new().unwrap();
                            let tx = tx.clone();

                            rt.spawn(async move {
                                match tx.send(res).await {
                                    Ok(_) => {}
                                    Err(_) => {
                                        debug!("Failed to send directory event");
                                    }
                                }
                            });
                        },
                        Config::default(),
                    )
                    .unwrap(),
                    watched_dirs: HashSet::new(),
                }));

                // spawn event handler
                tokio::spawn(Self::handle_directory_events(
                    rx,
                    Arc::clone(&dir_watcher),
                    Arc::clone(&cache),
                ));

                Some(dir_watcher)
            }
            WatchBehavior::DoNotMonitor => None,
        };

        HashCache { cache, dir_watcher }
    }

    /// Returns the hash of the file at the given path. If the file is not in the cache, it is
    /// calculated and added to the cache.
    ///
    /// Parameters:
    ///     path: The path to the file.
    ///     behavior: Whether to monitor the file for changes.
    ///
    /// Returns:
    ///    The hash of the file.
    pub async fn get_file_hash(&self, path: &Path, behavior: WatchBehavior) -> Result<String> {
        // fully resolve the path (symlinks, mapped drives, etc.)
        let resolved_path = path
            .canonicalize()
            .with_context(|| format!("Failed to resolve path '{}'", path.display()))?;

        let parent_dir = resolved_path
            .parent()
            .with_context(|| format!("Path '{}' has no parent", resolved_path.display()))?;

        // get file name
        let file_name = resolved_path
            .file_name()
            .with_context(|| format!("Path '{}' has no file name", resolved_path.display()))?
            .to_os_string()
            .into_string()
            .map_err(|_| anyhow::anyhow!("Failed to convert file name to string"))?;

        // get hashes for the parent directory
        trace!("Getting hashes for directory '{}'", parent_dir.display());
        let mut dir_hashes = self
            .cache
            .entry(parent_dir.to_path_buf())
            .or_insert_with(|| FileHashDict::new());

        match behavior {
            WatchBehavior::MonitorForChanges => {
                if let Some(dir_watcher) = &self.dir_watcher {
                    let mut dir_watcher = dir_watcher.lock().await;

                    // start watching the directory
                    HashCache::watch_dir(&parent_dir, &mut dir_watcher);
                }
            }
            WatchBehavior::DoNotMonitor => {}
        }

        if let Some(hash_entry) = dir_hashes.get(&file_name) {
            // if using timestamps, check if the file has been modified
            if !self.dir_watcher.is_none()
                || self.get_file_last_modified(&resolved_path)? == hash_entry.last_modified
            {
                trace!("Found file '{}' in cache", resolved_path.display());
                return Ok(hash_entry.hash.clone());
            }
        }

        // not found => calculate hash
        trace!("Calculating hash for file '{}'", resolved_path.display());
        let file_hash = Self::calculate_hash(&resolved_path)?;
        dir_hashes.insert(
            file_name,
            HashEntry {
                hash: file_hash.clone(),
                last_modified: self.get_file_last_modified(&resolved_path)?,
            },
        );

        Ok(file_hash)
    }

    pub async fn clear(&self) {
        if let Some(dir_watcher) = &self.dir_watcher {
            let mut dir_watcher = dir_watcher.lock().await;

            for dir in self.cache.iter().map(|entry| entry.key().clone()) {
                if let Err(e) = dir_watcher.watcher.unwatch(&dir) {
                    error!("Failed to unwatch directory: {:?}", e);
                }
            }
        }

        // clear the cache
        self.cache.clear();
    }

    /// Handle filesystem events
    async fn handle_directory_events(
        mut rx: mpsc::Receiver<notify::Result<Event>>,
        dir_watcher: Arc<Mutex<DirWatcher>>,
        cache: Arc<DirectoryToFileHashDict>,
    ) {
        while let Some(res) = rx.recv().await {
            match res {
                Ok(event) => {
                    // handle modify or remove events
                    if let EventKind::Modify(_) | EventKind::Remove(_) = event.kind {
                        // loop over paths in event
                        for path in event.paths {
                            trace!(
                                "Received modify / remove event for path '{}'",
                                path.display()
                            );

                            // get parent directory
                            let parent_dir = match path.parent() {
                                Some(parent) => parent,
                                None => {
                                    debug!("Path '{}' has no parent", path.display());
                                    continue;
                                }
                            };

                            // get file name
                            let file_name: String = match path
                                .file_name()
                                .and_then(|name| name.to_os_string().into_string().ok())
                            {
                                Some(name) => name,
                                None => {
                                    debug!("Path '{}' has no file name", path.display());
                                    continue;
                                }
                            };

                            // remove file from cache
                            let stop_watching_dir = match cache.get_mut(parent_dir) {
                                Some(mut dir_hashes) => {
                                    dir_hashes.remove(&file_name);
                                    trace!("Removed file '{}' from cache", path.display());
                                    dir_hashes.is_empty()
                                }
                                None => {
                                    debug!("Directory '{}' is not in cache", parent_dir.display());
                                    continue;
                                }
                            };

                            if stop_watching_dir {
                                trace!(
                                    "No more hashes in directory '{}', unwatching",
                                    parent_dir.display()
                                );

                                cache.remove(parent_dir);

                                let mut dir_watcher = dir_watcher.lock().await;

                                dir_watcher.watcher.unwatch(parent_dir).unwrap_or_else(|e| {
                                    debug!("Failed to unwatch directory: {:?}", e)
                                });
                                dir_watcher.watched_dirs.remove(parent_dir);
                            }
                        }
                    }
                }
                Err(e) => debug!("watch error: {:?}", e),
            }
        }

        trace!("Directory event handler terminated");
    }

    /// Watch a directory for changes
    fn watch_dir(directory: &Path, dir_watcher: &mut DirWatcher) {
        let directory = directory.to_path_buf();

        // get watcher and set of watched directories
        let watched_dirs = &mut dir_watcher.watched_dirs;

        if watched_dirs.insert(directory.clone()) {
            // watch directory
            match dir_watcher
                .watcher
                .watch(&directory, RecursiveMode::NonRecursive)
            {
                Ok(_) => {
                    trace!("Watching directory '{}'", directory.display());
                }
                Err(e) => {
                    debug!(
                        "Failed to watch directory '{}': {:?}",
                        directory.display(),
                        e
                    );
                }
            }
        }
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

    /// Get the last modified timestamp of a file
    fn get_file_last_modified(&self, resolved_path: &PathBuf) -> Result<SystemTime> {
        let metadata = std::fs::metadata(resolved_path)?;
        Ok(metadata.modified()?)
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
