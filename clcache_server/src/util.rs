use std::{io, path::Path, ptr::null_mut};
use widestring::WideCString;
use winapi::um::{
    handleapi::CloseHandle,
    processthreadsapi::{CreateProcessW, PROCESS_INFORMATION, STARTUPINFOW},
    winbase::{CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, DETACHED_PROCESS},
};

pub fn to_wide_cstring(s: impl AsRef<str>) -> Result<widestring::U16CString, io::Error> {
    WideCString::from_str(&s)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, format!("{}", e)))
}

pub fn osstr_to_wide_cstring(
    s: impl AsRef<std::ffi::OsStr>,
) -> Result<widestring::U16CString, io::Error> {
    WideCString::from_os_str(&s)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, format!("{}", e)))
}

pub fn create_process(app_name: &Path, command_line: &str) -> io::Result<()> {
    let app_name = osstr_to_wide_cstring(app_name.as_os_str())?;
    let mut command_line = to_wide_cstring(command_line)?;

    let mut si: STARTUPINFOW = unsafe { std::mem::zeroed() };
    let mut pi: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };

    si.cb = std::mem::size_of::<STARTUPINFOW>() as u32;

    let success = unsafe {
        CreateProcessW(
            app_name.as_ptr(),
            command_line.as_mut_ptr(),
            null_mut(), // process security attributes
            null_mut(), // primary thread security attributes
            0,          // handles are not inherited
            DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            null_mut(), // use parent's environment
            null_mut(), // use parent's current directory
            &mut si,
            &mut pi,
        )
    };

    if success == 0 {
        return Err(io::Error::last_os_error());
    }

    // Close handles to the child process and its main thread
    unsafe {
        CloseHandle(pi.hProcess);
        CloseHandle(pi.hThread);
    }

    Ok(())
}
