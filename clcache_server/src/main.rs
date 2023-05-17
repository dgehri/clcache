use anyhow::Result;
use clap::Parser;
use log::{debug, error, info};
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

/// Lightweight server to calculate MD5 hashes of files.
#[derive(Parser, Debug)]
#[command(author, version, about, long_about = None)]
struct Args {
    /// Server idle timeout in seconds.
    #[arg(long = "idle-timeout", default_value = "180")]
    timeout: u64,

    /// Sets non-default ID to be used by the server (for testing purposes)
    #[arg(
        long = "id",
        required = false,
        default_value = "626763c0-bebe-11ed-a901-0800200c9a66-2"
    )]
    id: String,

    /// Set verbosity level (repeat for more verbose output)
    #[arg(long = "verbose", short = 'v', action = clap::ArgAction::Count)]
    verbose: u8,
}

#[tokio::main(flavor = "multi_thread")]
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

    let timeout = Duration::from_secs(args.timeout);

    // Create the hash cache.
    let cache = Arc::new(hash_cache::HashCache::new());

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
            .map(|path: &str| PathBuf::from(path))
            .collect();

        // request all hashes
        let hashes = cache.get_file_hashes(&paths).await;

        match hashes {
            Ok(hashes) => {
                // Write the response.
                let mut response = Vec::<u8>::new();
                for hash in hashes {
                    response.extend(hash.as_bytes());
                    response.push(b'\n');
                }

                response.push(b'\0');

                client.write_all(&response).await?;
            }
            Err(e) => {
                // Write the error response.
                let mut response = Vec::<u8>::new();
                response.push(b'!'); // Error indicator
                response.extend(e.to_string().as_bytes());
                response.push(b'\0');

                client.write_all(&response).await?;
            }
        };
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    fn get_test_files(
        root_dir: &Path,
        count: usize,
        result: &mut Vec<PathBuf>,
    ) -> std::io::Result<()> {
        let mut entries = std::fs::read_dir(root_dir)?;
        while let Some(entry) = entries.next() {
            if let Ok(entry) = entry {
                let path = entry.path();
                if path.is_dir() {
                    get_test_files(&path, count, result).ok();
                } else {
                    // skip if less than 1MB and more than 20 MB
                    let metadata = std::fs::metadata(&path)?;
                    if metadata.len() < 100 * 1024 || metadata.len() > 1 * 1024 * 1024 {
                        continue;
                    }

                    // skip if no read access
                    if std::fs::File::open(&path).is_err() {
                        continue;
                    }

                    result.push(path);
                }
                if result.len() >= count as usize {
                    break;
                }
            }
        }

        Ok(())
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn performance_test_file_watcher() {
        let mut test_files = Vec::new();
        get_test_files(Path::new("C:\\"), 1000, &mut test_files).unwrap();

        let start = std::time::Instant::now();
        let cache = std::sync::Arc::new(crate::hash_cache::HashCache::new());
        let hashes = cache.get_file_hashes(&test_files).await.unwrap();

        println!(
            "Hashed {} files in {} ms (parallel)",
            hashes.len(),
            start.elapsed().as_millis()
        );
    }
}
