use std::{collections::HashMap, path::PathBuf, time::Duration};

use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::windows::named_pipe::{ClientOptions, NamedPipeClient},
    process::Command,
    time,
};
use winapi::shared::winerror::ERROR_PIPE_BUSY;

#[tokio::test]
async fn test_server() {
    let server_id = uuid::Uuid::new_v4().to_string();
    let pipe_name = format!(r"\\.\pipe\\LOCAL\\clcache-{}", server_id);
    let event_name = std::ffi::CString::new(format!(r"Local\ready-{}", server_id)).unwrap();

    // get project folder
    let project_folder = std::env::current_dir().unwrap();

    // combine with clcache_server.exe path
    #[cfg(debug_assertions)]
    let server_path = project_folder.join("target/debug/clcache_server.exe");
    #[cfg(not(debug_assertions))]
    let server_path = project_folder.join("target/release/clcache_server.exe");

    // Create event
    let event = unsafe {
        winapi::um::synchapi::CreateEventA(std::ptr::null_mut(), 0, 0, event_name.as_ptr())
    };

    // Launch the clcache_server.exe process found in the target directory.
    // The server will listen on the pipe name specified in the constant PIPE_NAME.
    let _server = Command::new(server_path)
        .arg("--idle-timeout=10")
        .arg(format!("--id={}", server_id))
        .spawn()
        .expect("Failed to start server");

    // Wait up to 5 seconds until event is set (exit if timeout)
    let now = time::Instant::now();
    let wait_result = unsafe { winapi::um::synchapi::WaitForSingleObject(event, 5000) };
    if wait_result != winapi::um::winbase::WAIT_OBJECT_0 {
        panic!("Server did not start in time.");
    } else {
        println!("Server started in {} ms", now.elapsed().as_millis());
    }

    // map of expected file names and their hashes
    let expected_map: HashMap<&str, &str> = HashMap::from([
        (
            "qjsonrpcabstractserver.h",
            "0cbdcb5aefc22f463528b8e931604147",
        ),
        ("qjsonrpcglobal.h", "58d68e85d7788e7e07b480431496b668"),
        ("qjsonrpcmessage.h", "6b742f5b5d13201547646fdd991f1145"),
        ("qjsonrpcservice.h", "1e69f8ad0d5e16cad26ab3bb454cf841"),
        (
            "qjsonrpcserviceprovider.h",
            "31bc9f3351c83bf468c21db7fbacb8b3",
        ),
        ("qjsonrpcsocket.h", "0f483597162c942713158b740fed5892"),
    ]);

    let test_file_base = project_folder.join("tests/res/");

    let test_file_set_1 = expected_map
        .iter()
        .take(3)
        .map(|(k, _)| test_file_base.join(k));
    let hash_set_1 = get_file_hashes(&pipe_name, &test_file_set_1.collect()).await;

    // check that the hashes are correct
    for (file_path, hash) in hash_set_1.iter() {
        assert_eq!(
            hash,
            expected_map[file_path.file_name().unwrap().to_str().unwrap()]
        );
    }

    let test_file_set_2 = expected_map
        .iter()
        .skip(3)
        .map(|(k, _)| test_file_base.join(k));
    let hash_set_2 = get_file_hashes(&pipe_name, &test_file_set_2.collect()).await;

    // check that the hashes are correct
    for (file_path, hash) in hash_set_2.iter() {
        assert_eq!(
            hash,
            expected_map[file_path.file_name().unwrap().to_str().unwrap()]
        );
    }

    // Terminate server (don't wait for response)
    println!("Terminating server...");
    let mut client = connect_to_server(&pipe_name).await;
    client.write_all(b"*exit\0").await.unwrap();
    let mut response = Vec::new();
    client.read_to_end(&mut response).await.unwrap();

}

async fn connect_to_server(pipe_name: &str) -> NamedPipeClient {
    loop {
        match ClientOptions::new().open(&pipe_name) {
            Ok(client) => break client,
            Err(e) if e.raw_os_error() == Some(ERROR_PIPE_BUSY as i32) => (),
            Err(e) => panic!("Failed to connect to server: {:?}", e),
        }

        time::sleep(Duration::from_millis(50)).await;
    }
}

async fn get_file_hashes(
    pipe_name: &str,
    files: &Vec<PathBuf>,
) -> HashMap<PathBuf, String> {

    // connect to server
    let mut client = connect_to_server(pipe_name).await;

    // pack all file paths into a single query, separated by '\n'
    let mut query = Vec::new();
    for file_path in files {
        query.extend_from_slice(file_path.to_str().unwrap().as_bytes());
        query.push(b'\n');
    }
    query.push(b'\0');

    println!("Sending query...");
    client.write_all(&query).await.unwrap();

    // read entire response into buffer
    println!("Reading response...");
    let mut response = Vec::new();
    client.read_to_end(&mut response).await.unwrap();

    // if response starts with '!', then an error occurred
    if response[0] == b'!' {
        panic!(
            "Server returned error: {}",
            String::from_utf8_lossy(&response[1..])
        );
    }

    // response is a list of hashes, separated by '\n'
    // the hashes will be returned in the same order as the files were sent
    let mut result = HashMap::new();
    for line in response.split(|&c| c == b'\n') {
        if line[0] == b'\0' {
            break;
        }

        let hash = String::from_utf8_lossy(line);
        let file_path = files[result.len()].clone();

        println!("{}: {}", file_path.file_name().unwrap().to_str().unwrap(), hash);

        result.insert(file_path, hash.to_string());
    }

    result
}
