use anyhow::Result;
use log::{error, info, trace};
use std::path::Path;
use std::process::Stdio;
use std::sync::Once;
use std::{collections::HashMap, path::PathBuf, str::FromStr};
use tokio::io::{self, BufReader, BufWriter};
use tokio::process::Child;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
};

static INIT: Once = Once::new();

#[tokio::test(flavor = "multi_thread", worker_threads = 16)]
async fn test_server() {
    INIT.call_once(|| {
        env_logger::builder()
            .filter_module("integration_test", log::LevelFilter::Trace)
            .format(|buf, record| {
                use std::io::Write;
                writeln!(
                    buf,
                    "[{},{:?}] [{}] - {}",
                    chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f"),
                    std::thread::current().id(),
                    record.level(),
                    record.args()
                )
            })
            .is_test(true)
            .try_init()
            .unwrap();
    });

    // get project folder
    let project_folder = std::env::current_dir().unwrap();

    // quickly spawn 100 clients to test the server's ability to handle multiple clients
    let mut handles = Vec::new();
    for _ in 0..100 {
        let project_folder = project_folder.clone();
        handles.push(tokio::spawn(async move {
            run_client_test(&project_folder).await;
        }));
    }

    // wait for all clients to finish
    for handle in handles {
        handle.await.unwrap();
    }

    // Terminate server (don't wait for response)
    info!("Terminating server...");
    match connect_to_server(&project_folder).await {
        Ok((mut child, mut stdin, mut _stdout)) => {
            let _ = stdin.write_all(b"exit\n").await;
            let _ = child.kill().await;
            let _ = child.wait().await;
        }
        Err(e) => {
            error!("Failed to connect to server: {}", e);
        }
    }
}

async fn run_client_test(project_folder: &Path) {
    // map of expected file names and their hashes
    let test_files = vec![
        ("1/qjsonrpcservice.h", "1e69f8ad0d5e16cad26ab3bb454cf841"),
        (
            "1/qjsonrpcserviceprovider.h",
            "31bc9f3351c83bf468c21db7fbacb8b3",
        ),
        ("1/qjsonrpcsocket.h", "0f483597162c942713158b740fed5892"),
        (
            "2/qjsonrpcabstractserver.h",
            "0cbdcb5aefc22f463528b8e931604147",
        ),
        ("2/qjsonrpcglobal.h", "58d68e85d7788e7e07b480431496b668"),
        ("2/qjsonrpcmessage.h", "6b742f5b5d13201547646fdd991f1145"),
    ];

    let expected_map: HashMap<String, &str> = test_files
        .iter()
        .map(|(k, v)| {
            (
                PathBuf::from_str(k)
                    .unwrap()
                    .file_name()
                    .unwrap()
                    .to_str()
                    .unwrap()
                    .to_owned(),
                *v,
            )
        })
        .collect();

    let test_file_base = project_folder.join("tests/res/");

    let test_file_set_1 = test_files
        .iter()
        .take(3)
        .map(|(k, _)| test_file_base.join(k));
    let hash_set_1 = get_file_hashes(project_folder, &test_file_set_1.collect()).await;

    // check that the hashes are correct
    for (file_path, hash) in hash_set_1.iter() {
        assert_eq!(
            hash,
            expected_map[file_path.file_name().unwrap().to_str().unwrap()]
        );
    }

    let test_file_set_2 = test_files
        .iter()
        .skip(3)
        .map(|(k, _)| test_file_base.join(k));
    let hash_set_2 = get_file_hashes(project_folder, &test_file_set_2.collect()).await;

    // check that the hashes are correct
    for (file_path, hash) in hash_set_2.iter() {
        assert_eq!(
            hash,
            expected_map[file_path.file_name().unwrap().to_str().unwrap()]
        );
    }

    get_file_hashes(project_folder, &vec![PathBuf::from("foo")]).await;

    // create a temporary folder with a file in it
    let temp_dir = tempfile::tempdir().unwrap();
    let temp_file_path = temp_dir.path().join("foo");
    std::fs::write(&temp_file_path, "foo").unwrap();

    // request the hash of the file
    let hash1 = get_file_hashes(project_folder, &vec![temp_file_path.clone()]).await;

    // modify the file
    info!("Modifying file");
    std::fs::write(&temp_file_path, "bar").unwrap();

    // request the hash of the file
    let hash2 = get_file_hashes(project_folder, &vec![temp_file_path.clone()]).await;

    // ensure that the hash has changed
    assert_ne!(hash1, hash2);

    // request the hash of the file
    let hash3 = get_file_hashes(project_folder, &vec![temp_file_path.clone()]).await;

    // ensure that the hash has not changed
    assert_eq!(hash2, hash3);
}

// launch executable in target directory with argument --client-mode and return a handle to stdin/stdout
async fn connect_to_server(
    project_folder: &Path,
) -> Result<
    (
        Child,
        BufWriter<tokio::process::ChildStdin>,
        BufReader<tokio::process::ChildStdout>,
    ),
    io::Error,
> {
    let server_id = uuid::Uuid::new_v4().to_string();

    // combine with clcache_server.exe path
    #[cfg(debug_assertions)]
    let server_path = project_folder.join("target/debug/clcache_server.exe");
    #[cfg(not(debug_assertions))]
    let server_path = project_folder.join("target/release/clcache_server.exe");

    // Launch the clcache_server.exe process found in the target directory.
    let mut child = Command::new(server_path)
        .arg("--idle-timeout=10")
        .arg(format!("--id={}", server_id))
        .arg("--client-mode")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("Failed to start server");

    let stdin = child.stdin.take().ok_or(io::Error::new(
        io::ErrorKind::BrokenPipe,
        "Failed to open stdin",
    ))?;
    let stdin = BufWriter::new(stdin);

    let stdout = child.stdout.take().ok_or(io::Error::new(
        io::ErrorKind::BrokenPipe,
        "Failed to open stdout",
    ))?;
    let stdout = BufReader::new(stdout);

    Ok((child, stdin, stdout))
}

async fn get_file_hashes(project_folder: &Path, files: &Vec<PathBuf>) -> HashMap<PathBuf, String> {
    // pack all file paths into a single query, separated by '\n'
    let mut query = Vec::new();
    for file_path in files {
        trace!("Sending file path: {:?}", file_path);
        // append file path to query, followed by '\n'
        query.extend(file_path.to_str().unwrap().as_bytes());
        query.extend(b"\n");
    }
    query.extend(b"\n");

    // connect to server
    match connect_to_server(project_folder).await {
        Ok((mut child, mut stdin, mut stdout)) => {
            // send query to server
            stdin.write_all(&query).await.unwrap();
            stdin.flush().await.unwrap();

            // read entire response into buffer
            let mut response = Vec::new();
            stdout.read_to_end(&mut response).await.unwrap();

            // if response starts with '!', then an error occurred
            if response[0] == b'!' {
                error!(
                    "Server returned error: {}",
                    String::from_utf8_lossy(&response[1..])
                );
                {}
            }

            // response is a list of hashes, separated by '\n'
            // the hashes will be returned in the same order as the files were sent
            let mut result = HashMap::new();
            for line in response.split(|&c| c == b'\n') {
                if line.is_empty() {
                    break;
                }

                let hash = String::from_utf8_lossy(line);
                let file_path = files.get(result.len()).unwrap().clone();

                // strip trailing '?' from file_path
                let file_path = if file_path.to_str().unwrap().ends_with('?') {
                    PathBuf::from(file_path.to_str().unwrap().trim_end_matches('?'))
                } else {
                    file_path
                };

                result.insert(file_path, hash.to_string());
            }

            // wait for server to exit
            child.wait().await.unwrap();

            result
        }
        Err(e) => {
            error!("Failed to connect to server: {}", e);
            HashMap::new()
        }
    }
}
