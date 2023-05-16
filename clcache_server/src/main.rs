use anyhow::Result;
use clap::Parser;
use log::{debug, error, info, trace};
use single_instance::SingleInstance;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use std::u8;
use tokio::io::{self, AsyncReadExt, AsyncWriteExt};
use tokio::net::windows::named_pipe::{NamedPipeServer, ServerOptions};
use tokio::sync::mpsc;
use tokio::time::{interval_at, Instant};

mod event;
mod hash_cache;

use event::signal_event;

#[derive(clap::ValueEnum, Clone, Debug)]
enum MonitoringMode {
    /// Use timestamp of the file to detect changes
    Timestamp,

    /// Watch filesystem for changes
    Watch,
}

/// Lightweight server to calculate MD5 hashes of files.
#[derive(Parser, Debug)]
#[command(author, version, about, long_about = None)]
struct Args {
    /// Server idle timeout in seconds.
    #[arg(long = "idle-timeout", default_value = "180")]
    timeout: u64,

    /// File monitoring mode
    #[arg(long = "monitoring-mode", default_value = "watch", value_enum)]
    monitoring_mode: MonitoringMode,

    /// Sets non-default ID to be used by the server (for testing purposes)
    #[arg(
        long = "id",
        required = false,
        default_value = "626763c0-bebe-11ed-a901-0800200c9a66-1"
    )]
    id: String,

    /// Set verbosity level (repeat for more verbose output)
    #[arg(long = "verbose", short = 'v', action = clap::ArgAction::Count)]
    verbose: u8,
}

#[tokio::main(flavor = "multi_thread", worker_threads = 32)]
async fn main() -> io::Result<()> {
    // Parse the command line arguments (accept --run-server=<timeout> parameter)
    // Get version from Cargo.toml
    let args = Args::parse();

    // Set verbosity level
    let verbosity = match args.verbose {
        0 => log::LevelFilter::Info,
        1 => log::LevelFilter::Debug,
        _ => log::LevelFilter::Trace,
    };
    let _ = env_logger::builder()
        .filter_module("clcache", verbosity)
        .format_timestamp_millis()
        .try_init();

    // Get the server ID from the command line arguments.
    let server_id = &args.id;
    let pipe_name = format!(r"\\.\pipe\\LOCAL\\clcache-{}", server_id);
    let server_ready_event = format!(r"Local\ready-{}", server_id);
    let singleton_name = format!(r"Local\singleton-{}", server_id);

    let instance = SingleInstance::new(&singleton_name).unwrap();
    if !instance.is_single() {
        info!("Another instance is already running.");
        return Ok(());
    }

    let watch_behavior = match args.monitoring_mode {
        MonitoringMode::Timestamp => hash_cache::WatchBehavior::DoNotMonitor,
        MonitoringMode::Watch => hash_cache::WatchBehavior::MonitorForChanges,
    };

    let timeout = Duration::from_secs(args.timeout);

    // Create the hash cache.
    let cache = Arc::new(hash_cache::HashCache::new(watch_behavior));

    // Create a channel to notify the main task when a client has connected.
    let (reset_idle_timer_tx, mut reset_idle_timer_rx) = mpsc::channel(1);

    // Create a channel to notify the main thread when server needs to exit.
    let (exit_tx, mut exit_rx) = mpsc::channel(1);

    // Create pipe server.
    tokio::spawn(async move {
        let mut server = ServerOptions::new()
            .first_pipe_instance(true)
            .create(&pipe_name)?;

        // Signal that we are ready by opening an existing WIN32 event and setting it.
        signal_event(&server_ready_event);

        // Log that we are ready to console, with the idle timeout
        info!(
            "Hash server is ready with idle timeout of {} seconds.",
            timeout.as_secs()
        );
        info!("Press Ctrl+C to exit.");

        loop {
            // Wait for a client to connect.
            server.connect().await?;

            // Copy the connected server to a new variable so that it can be moved into the task.
            let connected_server = server;

            // Create a new server to handle the next connection.
            server = ServerOptions::new().create(&pipe_name)?;

            // Reset the idle timer.
            reset_idle_timer_tx.send(()).await.ok();

            let exit_tx = exit_tx.clone();
            let cache_clone = Arc::clone(&cache);

            tokio::spawn(async move {
                if let Err(e) = handle_client(cache_clone, connected_server, exit_tx).await {
                    error!("Error in handle_client: {}", e);
                }

                Ok::<(), io::Error>(())
            });
        }

        #[allow(unreachable_code)]
        Ok::<(), io::Error>(())
    });

    let mut interval = interval_at(Instant::now() + timeout, timeout);

    loop {
        tokio::select! {
            _ = interval.tick() => {
                debug!("No connection for {} seconds. Exiting...", timeout.as_secs());
                break;
            }
            _ = reset_idle_timer_rx.recv() => {
                interval = interval_at(Instant::now() + timeout, timeout);
            }
            _ = exit_rx.recv() => {
                debug!("Received exit signal");
                break;
            }
            _ = tokio::signal::ctrl_c() => {
                break;
            }
        }
    }

    info!("Hash server terminated.");

    Ok(())
}

/// Handles a client connection.
async fn handle_client(
    cache: Arc<hash_cache::HashCache>,
    mut client: NamedPipeServer,
    exit_tx: mpsc::Sender<()>,
) -> Result<()> {
    // Read available data from the client, chunk by chunk, until we reach a zero byte.
    let mut read_buf = Vec::new();
    loop {
        let mut buf = vec![0; 1024];
        let read_len = client.read(&mut buf).await?;
        if read_len == 0 {
            // Client disconnected.
            return Ok(());
        }

        // If the last byte is zero, then we have reached the end of the message.
        if buf[read_len - 1] == 0 {
            read_buf.extend(&buf[..read_len]);
            break;
        }

        read_buf.extend(&buf[..read_len]);
    }

    // If message starts with "*", then it's a command.
    if read_buf[0] == b'*' {
        let command = String::from_utf8(read_buf[1..read_buf.len() - 1].to_vec())?;
        match command.as_str() {
            "clear" => {
                // Reset the cache.
                cache.clear().await;

                // Echo the command back to the client.
                client.write_all(b"*ok\n").await?;
            }
            "exit" => {
                // Echo the command back to the client.
                client.write_all(b"*ok\n").await?;

                // Terminate the server.
                exit_tx.send(()).await.unwrap();
            }
            _ => {
                // Unknown command.
                client.write_all(b"Unknown command").await?;
            }
        }
    } else {
        // Convert the list of paths to a vector of PathBufs:
        // - if path ends in '?', strip the '?' and set WatchBehavior to DoNotMonitor
        // - otherwise, set WatchBehavior to MonitorForChanges
        let paths: Vec<_> = String::from_utf8(read_buf[..read_buf.len() - 1].to_vec())?
            .lines()
            .map(|path: &str| {
                if path.ends_with('?') {
                    (
                        PathBuf::from(&path[..path.len() - 1]),
                        hash_cache::WatchBehavior::DoNotMonitor,
                    )
                } else {
                    (
                        PathBuf::from(path),
                        hash_cache::WatchBehavior::MonitorForChanges,
                    )
                }
            })
            .collect();

        // Prepare the response buffer.
        let mut response = Vec::new();

        // Iterate over the paths and calculate the hashes.
        for (path, watch_behavior) in paths {
            trace!("Calculating hash for {}", path.display());
            match cache.get_file_hash(&path, watch_behavior).await {
                Ok(file_hash) => response.extend(file_hash.as_bytes()),
                Err(e) => {
                    response.push(b'!'); // Error indicator
                    response.extend(e.to_string().as_bytes());
                }
            }
            response.push(b'\n');
        }

        response.push(0);
        client.write_all(&response).await?;
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use rand::{Rng, RngCore};
    use std::{fs, io::Write, path::PathBuf};

    use crate::hash_cache;

    struct TestFiles {
        temp_dir: tempfile::TempDir,
        test_files: Vec<PathBuf>,
        files_to_modify: Vec<PathBuf>,
    }

    impl TestFiles {
        fn new() -> Self {
            // create a temp directory
            let temp_dir = tempfile::tempdir().unwrap();

            // inside, create 10000 files with random content, evenly spread over 100 directories
            // and mark 50 files for later modification
            let mut rng = rand::thread_rng();
            let mut files_to_modify = Vec::new();
            let mut test_files = Vec::new();
            for i in 0..10 {
                let dir = temp_dir.path().join(format!("dir{}", i));
                fs::create_dir(&dir).unwrap();
                for j in 0..100 {
                    let file = dir.join(format!("file{}", j));
                    test_files.push(file.clone());
                    let mut f = fs::File::create(&file).unwrap();
                    let mut buf = [0; 1024];
                    rng.fill_bytes(&mut buf);
                    f.write_all(&buf).unwrap();
                    if rng.gen_bool(0.005) {
                        files_to_modify.push(file);
                    }
                }
            }

            TestFiles {
                temp_dir,
                test_files,
                files_to_modify,
            }
        }

        fn info(&self) {
            println!("Will be modifying {} files", self.files_to_modify.len());

            // count number of unique directories for modified files
            let mut unique_dirs = std::collections::HashSet::new();
            for file in &self.files_to_modify {
                unique_dirs.insert(file.parent().unwrap().to_path_buf());
            }
            println!("Will be modifying {} directories", unique_dirs.len());
        }
    }

    #[tokio::test]
    async fn performance_test_file_watcher() {
        let test_files = TestFiles::new();
        test_files.info();

        test_impl(&test_files, hash_cache::WatchBehavior::DoNotMonitor).await;
        test_impl(&test_files, hash_cache::WatchBehavior::MonitorForChanges).await;
        test_impl(&test_files, hash_cache::WatchBehavior::DoNotMonitor).await;
        test_impl(&test_files, hash_cache::WatchBehavior::MonitorForChanges).await;
    }

    async fn test_impl(test_files: &TestFiles, watch_behavior: hash_cache::WatchBehavior) {
        println!("Measuring performance for {:?}", watch_behavior);

        let cache = std::sync::Arc::new(crate::hash_cache::HashCache::new(watch_behavior));

        // record current time
        let start = std::time::Instant::now();

        // request hashes for all files
        for file in &test_files.test_files {
            cache
                .get_file_hash(file, crate::hash_cache::WatchBehavior::MonitorForChanges)
                .await
                .unwrap();
        }

        // print elapsed time
        println!("Elapsed time: {:?}", start.elapsed());

        // modify files
        for file in &test_files.files_to_modify {
            let mut f = fs::File::create(file).unwrap();
            let mut buf = [0; 1024];
            rand::thread_rng().fill_bytes(&mut buf);
            f.write_all(&buf).unwrap();
        }

        // record current time
        let start = std::time::Instant::now();

        // request hashes for all files
        for file in &test_files.test_files {
            cache
                .get_file_hash(file, crate::hash_cache::WatchBehavior::MonitorForChanges)
                .await
                .unwrap();
        }

        // print elapsed time
        println!("Elapsed time: {:?}", start.elapsed());
    }
}
