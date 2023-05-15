pub(crate) use anyhow::{Context, Result};

use log::{debug, error, trace};
use notify::{Config, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};

use std::{
    collections::{HashMap, HashSet},
    fs::File,
    io::BufRead,
    path::{Path, PathBuf},
    sync::Arc,
};

use tokio::{
    runtime::Runtime,
    sync::{mpsc, Mutex},
};

use dashmap::DashMap;

type FileHashDict = HashMap<String, String>;
type DirectoryToFileHashDict = DashMap<PathBuf, FileHashDict>;

pub enum WatchBehavior {
    MonitorForChanges, // watch file and remove from cache if it changes
    DoNotMonitor,      // do not watch file
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
    dir_watcher: Arc<Mutex<DirWatcher>>,
}

impl HashCache {
    pub fn new() -> Self {
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

        let cache = Arc::new(DirectoryToFileHashDict::new());

        // spawn event handler
        tokio::spawn(Self::handle_directory_events(
            rx,
            Arc::clone(&dir_watcher),
            Arc::clone(&cache),
        ));

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
                // start watching the directory
                self.watch_dir(&parent_dir).await;
            }
            WatchBehavior::DoNotMonitor => {
                trace!("Not watching directory '{}'", parent_dir.display());
            }
        }

        // look up file in it
        if let Some(file_hash) = dir_hashes.get(&file_name) {
            trace!("Found file '{}' in cache", resolved_path.display());
            return Ok(file_hash.clone());
        }

        // not found => calculate hash
        trace!("Calculating hash for file '{}'", resolved_path.display());
        let file_hash = Self::calculate_hash(&resolved_path)?;
        dir_hashes.insert(file_name, file_hash.clone());

        Ok(file_hash)
    }

    pub async fn clear(&self) {
        for dir in self.cache.iter().map(|entry| entry.key().clone()) {
            if let Err(e) = self.dir_watcher.lock().await.watcher.unwatch(&dir) {
                error!("Failed to unwatch directory: {:?}", e);
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

    async fn watch_dir(&self, directory: &Path) {
        let directory = directory.to_path_buf();

        // spawn a task to watch the directory (async in order to not block the main thread)
        let mut dir_watcher = self.dir_watcher.lock().await;

        // get watcher and set of watched directories
        let watched_dirs = &mut dir_watcher.watched_dirs;

        if !watched_dirs.contains(&directory) {
            // add directory to set of watched directories
            watched_dirs.insert(directory.clone());

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
}
