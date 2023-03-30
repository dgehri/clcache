import multiprocessing
import sys
from pathlib import Path

import win32con
import win32evtlogutil

from ..utils.logging import LogLevel


def log_win_event(message: str, log_level: LogLevel):  # sourcery skip: use-contextlib-suppress
    try:
        if log_win_event.program_name is None:
            log_win_event.program_name = Path(sys.argv[0]).stem

        if log_win_event.pid is None:
            log_win_event.pid = multiprocessing.current_process().pid

        source = log_win_event.program_name
        
        # translate log_level to event_type
        if log_level == LogLevel.TRACE:
            event_type = win32con.EVENTLOG_INFORMATION_TYPE
        elif log_level == LogLevel.DEBUG:
            event_type = win32con.EVENTLOG_INFORMATION_TYPE
        elif log_level == LogLevel.INFO:
            event_type = win32con.EVENTLOG_INFORMATION_TYPE
        elif log_level == LogLevel.WARN:
            event_type = win32con.EVENTLOG_WARNING_TYPE
        elif log_level == LogLevel.ERROR:
            event_type = win32con.EVENTLOG_ERROR_TYPE
        else:
            event_type = win32con.EVENTLOG_INFORMATION_TYPE
                                
        event_id = 1
        
        if not log_win_event.source:
            try:
                # Use win32evtlog.pyd in the same folder as this python script
                dll_path = Path(__file__).parent / "win32evtlog.pyd"                
                win32evtlogutil.AddSourceToRegistry(source, msgDLL=dll_path)
                log_win_event.source = True
            except Exception:
                pass
            
        win32evtlogutil.ReportEvent(
            appName=log_win_event.program_name, 
            eventID=event_id, 
            eventType=event_type, 
            strings=[message], 
            data=b"")
        
    except Exception:
        pass


log_win_event.program_name = None
log_win_event.pid = None
log_win_event.source = False

