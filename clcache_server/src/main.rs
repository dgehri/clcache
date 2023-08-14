use anyhow::Result;
use clap::Parser;
use event::signal_event;
use futures::stream::StreamExt;
use log::{debug, error, info};
use single_instance::SingleInstance;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use std::u8;
use tokio::io;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::windows::named_pipe::{ClientOptions, NamedPipeServer, ServerOptions};
use tokio::sync::mpsc;
use tokio::time::{self, interval_at, Instant};
use tokio_util::codec::{FramedRead, LinesCodec};
use util::create_process;
use util::to_wide_cstring;
use winapi::shared::winerror::ERROR_PIPE_BUSY;
use winapi::um::handleapi::CloseHandle;
use winapi::um::synchapi::{CreateEventW, CreateMutexW, OpenMutexW, WaitForSingleObject};
use winapi::um::winbase::{INFINITE, WAIT_OBJECT_0};
use winapi::um::winnt::SYNCHRONIZE;

mod event;
mod hash_cache;
mod util;

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

    /// Act as client and return hashes for paths given on stdin
    #[arg(long = "client-mode", required = false, default_value = "false")]
    client_mode: bool,

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
    let timeout = Duration::from_secs(args.timeout);

    if args.client_mode {
        return get_hashes_as_client(server_id, &singleton_name, &pipe_name, &timeout).await;
    }

    let instance = SingleInstance::new(&singleton_name).map_err(|e| {
        io::Error::new(
            io::ErrorKind::Other,
            format!("Error creating single instance: {}", e),
        )
    })?;

    if !instance.is_single() {
        info!("Another instance is already running.");
        return Ok(());
    }

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
            info!("Waiting for client to connect...");
            server.connect().await?;
            info!("Client connected.");

            // Copy the connected server to a new variable so that it can be moved into the task.
            let mut connected_server = server;

            // Create a new server to handle the next connection.
            info!("Creating new server...");
            server = match ServerOptions::new().create(&pipe_name) {
                Ok(s) => s,
                Err(e) => {
                    error!("Error creating new server: {}", e);
                    return Ok::<(), io::Error>(());
                }
            };

            // Reset the idle timer.
            reset_idle_timer_tx.send(()).await.ok();

            let exit_tx = exit_tx.clone();
            let cache_clone = Arc::clone(&cache);

            tokio::spawn(async move {
                if let Err(e) = handle_client(cache_clone, &mut connected_server, exit_tx).await {
                    // Handle disconnection if an error occurs in handle_client
                    let _ = connected_server.disconnect();
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
    client: &mut NamedPipeServer,
    exit_tx: mpsc::Sender<()>,
) -> Result<()> {
    const PIPE_TIMEOUT: Duration = Duration::from_secs(5);

    let mut read_buf = Vec::new();
    loop {
        let mut buf = vec![0; 4096];

        match tokio::time::timeout(PIPE_TIMEOUT, client.read(&mut buf)).await {
            Ok(Ok(read_len)) => {
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
            Ok(Err(e)) => {
                // Client disconnected.
                return Err(e.into());
            }
            Err(_) => {
                // Timeout.
                return Err(
                    io::Error::new(io::ErrorKind::TimedOut, "Client read timed out").into(),
                );
            }
        }
    }

    // If message starts with "*", then it's a command.
    let response = if read_buf[0] == b'*' {
        let command = String::from_utf8(read_buf[1..read_buf.len() - 1].to_vec())?;
        match command.as_str() {
            "clear" => {
                // Reset the cache.
                cache.clear().await;
                Some(b"*ok\n".to_vec())
            }
            "exit" => {
                // Echo the command back to the client.
                client.write_all(b"*ok\n").await?;

                // Terminate the server.
                exit_tx.send(()).await.unwrap();
                None
            }
            _ => {
                // Unknown command.
                Some(b"Unknown command\n\0".to_vec())
            }
        }
    } else {
        // Convert the list of paths to a vector of PathBufs:
        // - if path ends in '?', strip the '?' and set WatchBehavior to DoNotMonitor
        // - otherwise, set WatchBehavior to MonitorForChanges
        let paths: Vec<_> = String::from_utf8(read_buf[..read_buf.len() - 1].to_vec())?
            .lines()
            .map(PathBuf::from)
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
                Some(response)
            }
            Err(e) => {
                // Write the error response.
                let mut response = Vec::<u8>::new();
                response.push(b'!'); // Error indicator
                response.extend(e.to_string().as_bytes());
                response.push(b'\0');
                Some(response)
            }
        }
    };

    if let Some(response) = response {
        let result = tokio::time::timeout(PIPE_TIMEOUT, client.write_all(&response)).await;
        match result {
            Ok(Ok(_)) => {}
            Ok(Err(e)) => {
                // Client disconnected.
                return Err(e.into());
            }
            Err(_) => {
                // Timeout.
                return Err(
                    io::Error::new(io::ErrorKind::TimedOut, "Client write timed out").into(),
                );
            }
        }
        client.flush().await?;
    }

    Ok(())
}

async fn get_hashes_as_client(
    server_id: &str,
    singleton_name: &str,
    pipe_name: &str,
    server_idle_timeout: &Duration,
) -> io::Result<()> {
    // read hashes from stdin (read until empty line)
    let stdin = io::stdin();
    let mut reader = FramedRead::new(stdin, LinesCodec::new());
    let mut path_list = Vec::new();
    while let Some(line_result) = reader.next().await {
        match line_result {
            Ok(line) => {
                if line.is_empty() {
                    break;
                }

                // remove trailing whitespace / newlines
                let line = line.trim_end();
                path_list.push(line.to_string());
            }
            Err(e) => {
                eprintln!("Error reading line: {}", e);
            }
        }
    }

    // spawn server if needed
    spawn_server(server_id, singleton_name, server_idle_timeout).await?;

    let mut client = loop {
        match ClientOptions::new().open(pipe_name) {
            Ok(client) => break client,
            Err(e) if e.raw_os_error() == Some(ERROR_PIPE_BUSY as i32) => (),
            Err(e) => return Err(e),
        }

        time::sleep(Duration::from_millis(50)).await;
    };

    let mut message = path_list
        .into_iter()
        .collect::<Vec<_>>()
        .join("\n")
        .into_bytes();

    message.push(b'\0');
    client.write_all(&message).await?;

    // Read the response
    let mut response = Vec::new();
    client.read_to_end(&mut response).await?;

    // Print to stdout if the response is not empty
    print!(
        "{}",
        String::from_utf8_lossy(&response[..response.len() - 1])
    );
    Ok(())
}

/// Function to spawn the server.
pub async fn spawn_server(
    server_id: &str,
    singleton_name: &str,
    server_idle_timeout: &Duration,
) -> io::Result<()> {
    // Check if the server is already running
    if is_server_running(singleton_name)? {
        log::debug!("Server already running");
        return Ok(());
    }

    // Avoid double spawning using a named mutex
    let launch_mutex_name = format!("Local\\mutex-{}", server_id);
    let wide_string = to_wide_cstring(&launch_mutex_name)?;

    let mutex = unsafe { CreateMutexW(std::ptr::null_mut(), 1, wide_string.as_ptr()) };
    if mutex.is_null() {
        return Err(io::Error::last_os_error());
    }

    let wait_result = unsafe { WaitForSingleObject(mutex, INFINITE) };
    if wait_result != 0 {
        return Err(io::Error::last_os_error());
    }

    // Check again if the server is running after acquiring the mutex
    if is_server_running(singleton_name)? {
        return Ok(());
    }

    // Get the current executable's path
    let current_exe_path = std::env::current_exe().expect("Failed to get current exe path");

    // Launch the server with the required parameters
    let command_line = format!(
        "{} --idle-timeout={} --id={} -v -v -v -v",
        current_exe_path.to_string_lossy(),
        server_idle_timeout.as_secs(),
        server_id
    );
    create_process(&current_exe_path, &command_line)?;

    // Wait for the server to signal that it's ready
    let wait_duration = Duration::from_secs(10);
    let pipe_ready_event_name = format!("Local\\ready-{}", server_id);
    wait_for_ready_event(&pipe_ready_event_name, &wait_duration).await?;
    log::debug!(
        "Started hash server with timeout {} seconds",
        server_idle_timeout.as_secs()
    );
    Ok(())
}

async fn wait_for_ready_event(
    pipe_ready_event_name: &str,
    wait_duration: &Duration,
) -> io::Result<()> {
    let wide_string = to_wide_cstring(pipe_ready_event_name)?;
    let handle = unsafe { CreateEventW(std::ptr::null_mut(), 0, 0, wide_string.as_ptr()) };

    if handle.is_null() {
        return Err(io::Error::last_os_error());
    }

    let wait_result = unsafe { WaitForSingleObject(handle, wait_duration.as_millis() as u32) };
    unsafe { winapi::um::handleapi::CloseHandle(handle) };

    match wait_result == WAIT_OBJECT_0 {
        true => Ok(()),
        false => Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "Failed to start hash server",
        )),
    }
}

fn is_server_running(singleton_name: &str) -> io::Result<bool> {
    let wide_string = to_wide_cstring(singleton_name)?;
    let handle = unsafe { OpenMutexW(SYNCHRONIZE, 0, wide_string.as_ptr()) };

    if handle.is_null() {
        Ok(false)
    } else {
        unsafe { CloseHandle(handle) };
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    fn get_test_files(
        root_dir: &Path,
        count: usize,
        result: &mut Vec<PathBuf>,
    ) -> std::io::Result<()> {
        let entries = std::fs::read_dir(root_dir)?;
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                get_test_files(&path, count, result).ok();
            } else {
                // skip if less than 1MB and more than 20 MB
                let metadata = std::fs::metadata(&path)?;
                if metadata.len() < 100 * 1024 || metadata.len() > 1024 * 1024 {
                    continue;
                }

                // skip if no read access
                if std::fs::File::open(&path).is_err() {
                    continue;
                }

                result.push(path);
            }
            if result.len() >= count {
                break;
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
