use std::collections::HashMap;
use std::env;
use std::process::{Command, Stdio};

// define constant for STATUS_ACCESS_VIOLATION (as hex)
#[allow(overflowing_literals)]
const STATUS_ACCESS_VIOLATION: i32 = 0xC0000005 as i32;

fn main() {
    let mut env: HashMap<String, String> = env::vars().collect();

    // add CLCACHE_NO_SAFE_EXECUTE environment variable        
    env.insert("CLCACHE_NO_SAFE_EXECUTE".to_string(), "1".to_string());

    // try launching the child process, and check if it returns STATUS_ACCESS_VIOLATION, 
    // if so, try launching it again.
    let mut exit_code = launch(&env);
    if exit_code == STATUS_ACCESS_VIOLATION {

        // try again, but remove CLCACHE_COUCHBASE environment variable
        env.remove("CLCACHE_COUCHBASE");

        exit_code = launch(&env);
    }

    // Exit with the same status code as the child process
    std::process::exit(exit_code);
}

// Launch the child process using the environment passed as a parameter in "env", and return the exit code
fn launch(environment: &HashMap<String, String>) -> i32 {
    // Get the current executable's path
    let current_exe_path = env::current_exe().expect("Failed to get current executable's path");
    let parent_dir = current_exe_path.parent().expect("Failed to get parent directory");
    let current_exe_name = current_exe_path.file_name().expect("Failed to get file name");

    // Construct the executable path by inserting the "py" folder name
    let mut clcache_path = parent_dir.to_path_buf();
    clcache_path.push("py");
    clcache_path.push(current_exe_name);

    // Create the command with the same arguments
    let args = env::args().skip(1).collect::<Vec<_>>();
    let mut command = Command::new(clcache_path);
    command.args(&args);

    // Inherit StdOut and StdErr
    command.stdout(Stdio::inherit());
    command.stderr(Stdio::inherit());

    // set environment to environment passed as a parameter
    for (key, value) in environment {
        command.env(key, value);
    }

    // Execute the command
    let status = command
        .status()
        .expect("Failed to execute the command");

    // Return the exit code
    status.code().unwrap_or(1)
}
