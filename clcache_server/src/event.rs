use std::{ffi::OsStr, iter::once, os::windows::prelude::OsStrExt};
use winapi::um::winnt::EVENT_ALL_ACCESS;

/// Signals the event with the given name.
///
/// Parameters:
///    name: The name of the event to signal.
///
/// Returns:
///   Ok(()) if the event was signaled successfully, otherwise an error.
pub fn signal_event(name: &str) {
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
