use anyhow::{Context, Error, Result};
use clap::Parser;
use single_instance::SingleInstance;
use std::collections::HashMap;
use std::ffi::OsStr;
use std::fs::File;
use std::iter::once;
use std::os::windows::prelude::OsStrExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use std::io::BufRead;
use tokio::io::{self, AsyncReadExt, AsyncWriteExt};
use tokio::net::windows::named_pipe::{NamedPipeServer, ServerOptions};
use tokio::sync::mpsc;
use tokio::time::{interval_at, Instant};
use winapi::um::winnt::EVENT_ALL_ACCESS;

const BUFFER_SIZE: usize = 65536;

struct HashCache {
    /// Maps watched directories to a map of file names to hashes.
    cache: Arc<Mutex<HashMap<PathBuf, HashMap<String, String>>>>,
}

impl HashCache {
    pub fn new() -> Self {
        HashCache {
            cache: Arc::new(Mutex::new(HashMap::new())),
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
        let parent = path
            .parent()
            .with_context(|| format!("Path '{}' has no parent", path.display()))?;

        let name = path
            .file_name()
            .with_context(|| format!("Path '{}' has no file name", path.display()))?
            .to_str()
            .with_context(|| {
                format!(
                    "Path '{}' cannot be converted to UTF-8 string",
                    path.display()
                )
            })?;

        let mut cache = self
            .cache
            .lock()
            .map_err(|e| Error::msg(format!("Failed to lock cache: {:?}", e)))?;

        let watched_dir = cache.entry(parent.to_path_buf()).or_insert(HashMap::new());

        if let Some(file_hash) = watched_dir.get(name) {
            return Ok(file_hash.clone());
        }

        let file_hash = calculate_hash(path)?;
        watched_dir.insert(name.to_string(), file_hash.clone());

        Ok(file_hash)
    }
}

fn calculate_hash(path: &Path) -> Result<String> {
    let f = File::open(path).unwrap();

    // Find the length of the file
    let len = f.metadata().unwrap().len();

    // Decide on a reasonable buffer size (1MB in this case, fastest will depend on hardware)
    let buf_len = len.min(1_000_000) as usize;
    let mut buf = std::io::BufReader::with_capacity(buf_len, f);
    let mut context = md5::Context::new();
    loop {
        // Get a chunk of the file
        let part = buf.fill_buf().unwrap();

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

/// Handles a client connection.
async fn handle_client(
    cache: Arc<HashCache>,
    mut client: NamedPipeServer,
    reset_tx: mpsc::Sender<()>,
    exit_tx: mpsc::Sender<()>,
) -> Result<()> {
    // Notify the main task that a new client has connected
    reset_tx.send(()).await.unwrap();

    let mut read_buf = vec![0u8; BUFFER_SIZE];
    let mut read_len = 0;

    // Read the list of paths from the client.
    loop {
        let n = client.read(&mut read_buf[read_len..]).await?;
        read_len += n;
        if read_buf[read_len - 1] == 0 {
            break;
        }
    }

    // If message starts with "*", then it's a command.
    if read_buf[0] == b'*' {
        let command = String::from_utf8(read_buf[1..read_len - 1].to_vec())?;
        match command.as_str() {
            "reset" => {
                // Reset the cache.
                cache.cache.lock().unwrap().clear();

                // Echo the command back to the client.
                client.write_all(b"*reset\n").await?;
            }

            "exit" => {
                // Echo the command back to the client.
                client.write_all(b"*exit\n").await?;

                // Terminate the server.
                exit_tx.send(()).await.unwrap();
            }
            _ => {
                // Unknown command.
                client.write_all(b"Unknown command").await?;
            }
        }
    } else {
        // Convert the list of paths to a vector of PathBufs.
        let paths: Vec<PathBuf> = String::from_utf8(read_buf[..read_len - 1].to_vec())?
            .lines()
            .map(PathBuf::from)
            .collect();

        // Prepare the response buffer.
        let mut response = Vec::new();

        // Iterate over the paths and calculate the hashes.
        for path in paths {
            match cache.get_file_hash(&path).await {
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

/// Signals the event with the given name.
///
/// Parameters:
///    name: The name of the event to signal.
///
/// Returns:
///   Ok(()) if the event was signaled successfully, otherwise an error.
fn signal_event(name: &str) {
    let event_name_wide: Vec<u16> = OsStr::new(name).encode_wide().chain(once(0)).collect();

    let handle = unsafe {
        winapi::um::synchapi::OpenEventW(
            EVENT_ALL_ACCESS,
            winapi::shared::minwindef::FALSE,
            event_name_wide.as_ptr(),
        )
    };

    // check if handle is valid, and if yes, set event. Otherwise, do nothing.
    if handle != winapi::shared::ntdef::NULL {
        unsafe {
            winapi::um::synchapi::SetEvent(handle);
        }
    }

    // close handle
    unsafe {
        winapi::um::handleapi::CloseHandle(handle);
    }
}

/// Lightweight server to calculate MD5 hashes of files.
#[derive(Parser, Debug)]
#[command(author, version, about, long_about = None)]
struct Args {
    /// Server idle timeout in seconds.
    #[arg(long = "idle-timeout", default_value = "180")]
    timeout: u64,

    /// Sets non-default ID to be used by the server (for testing purposes)
    #[arg(long = "id", required = false)]
    id: String,
}

#[tokio::main]
async fn main() -> io::Result<()> {
    // Parse the command line arguments (accept --run-server=<timeout> parameter)
    // Get version from Cargo.toml
    let args = Args::parse();

    let mut server_id = "626763c0-bebe-11ed-a901-0800200c9a66-1";
    if !args.id.is_empty() {
        server_id = &args.id;
    }

    let pipe_name = format!(r"\\.\pipe\\LOCAL\\clcache-{}", server_id);
    let event_name = format!(r"Local\ready-{}", server_id);
    let singleton_name = format!(r"Local\singleton-{}", server_id);

    let instance = SingleInstance::new(&singleton_name).unwrap();
    if !instance.is_single() {
        println!("Another instance is already running.");
        return Ok(());
    }

    let timeout = Duration::from_secs(args.timeout);

    // Create the hash cache.
    let cache = Arc::new(HashCache::new());

    // Create a channel to notify the main task when a client has connected.
    let (reset_tx, mut reset_rx) = mpsc::channel(1);

    // Create a channel to notify the main thread when server needs to exit.
    let (exit_tx, mut exit_rx) = mpsc::channel(1);

    // Create pipe server.
    tokio::spawn(async move {
        let mut server = ServerOptions::new()
            .first_pipe_instance(true)
            .create(&pipe_name)?;

        // Signal that we are ready by opening an existing WIN32 event and setting it.
        signal_event(&event_name);

        // Log that we are ready to console, with the idle timeout
        println!(
            "Hash server is ready with idle timeout of {} seconds.",
            timeout.as_secs()
        );
        println!("Press Ctrl+C to exit.");

        loop {
            // Wait for a client to connect.
            server.connect().await?;

            // Copy the connected server to a new variable so that it can be moved into the task.
            let connected_server = server;

            // Create a new server to handle the next connection.
            server = ServerOptions::new().create(&pipe_name)?;

            let reset_tx = reset_tx.clone();
            let exit_tx = exit_tx.clone();
            let cache_clone = Arc::clone(&cache);

            tokio::spawn(async move {
                if let Err(e) = handle_client(cache_clone, connected_server, reset_tx, exit_tx).await {
                    eprintln!("Error in handle_client: {}", e);
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
                println!("No connection for {} seconds. Exiting...", timeout.as_secs());
                break;
            }
            _ = reset_rx.recv() => {
                println!("Resetting timeout");
                interval = interval_at(Instant::now() + timeout, timeout);
            }
            _ = exit_rx.recv() => {
                println!("Received exit signal");
                break;
            }
            _ = tokio::signal::ctrl_c() => {
                break;
            }
        }
    }

    println!("Hash server terminated.");

    Ok(())
}
