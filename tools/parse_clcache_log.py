from itertools import groupby
import re
from collections import defaultdict
from datetime import datetime

with open(r"D:\Users\Daniel\Downloads\clcache (7).log", "r") as file:
    log_data = file.readlines()

# Initialize variables
manifest_misses = []
processing_times = defaultdict(float)

# Typical line:
# [2023-03-31 15:15:40.063] [clcache 4.4.7-dgehri] [4604] [TRACE] Manifest entry miss, invoking real compiler
# Capture as timestamp, source, pid, log_level, message
line_re = re.compile(
    r"^\[(?P<timestamp>[0-9\-\.:\s]+)\]\s\[(?P<source>[^\]]+)\]\s\[(?P<pid>\d+)\]\s\[(?P<log_level>[^\]]+)\]\s(?P<message>.+)$"
)

# Parse log into a list of dictionaries, grouped by PID
log_data = [
    line_re.match(line).groupdict()
    for line in log_data
    if line_re.match(line.strip()) is not None
]
# Group by PID, mapping the timestamp to a datetime type
log_data_by_pid = [
    {
        "pid": pid,
        "lines": [
            {**line, "timestamp": datetime.strptime(line["timestamp"], "%Y-%m-%d %H:%M:%S.%f")}
            for line in lines
        ],
    }
    for pid, lines in groupby(log_data, key=lambda x: x["pid"])
]

# Calculate processing time for each PID
for pid_data in log_data_by_pid:
    # Get the first and last line for this PID
    first_line = pid_data["lines"][0]
    last_line = pid_data["lines"][-1]

    # Calculate processing time
    processing_time = (last_line["timestamp"] - first_line["timestamp"]).total_seconds()
    pid_data["processing_time"] = processing_time
    
# Extract the source file name for each PID (may be in any of the messages)
file_path_regex = re.compile(r"^Input files: (.+)$")
for pid_data in log_data_by_pid:
    # Find the first line with a source file path
    for line in pid_data["lines"]:
        if "Input files" in line["message"]:
            # Extract the source file path
            source_file_path = re.search(file_path_regex, line["message"])[1]
            pid_data["source_file_path"] = source_file_path
            break
    
# Extract the manifest misses
for pid_data in log_data_by_pid:
    # Find the first line with a manifest miss
    for line in pid_data["lines"]:
        if "Manifest entry miss" in line["message"]:
            manifest_misses.append(pid_data)
            break
        
# Print top 10 longest processing source files (title in bold, with empty line below)
print("\n\033[1mTop 10 longest processing source files:\033[0m")
for pid_data in sorted(log_data_by_pid, key=lambda x: x["processing_time"], reverse=True)[:10]:
    print(f"{pid_data['source_file_path']} - {pid_data['processing_time']:.2f}s")
    
print()
# Print manifest misses (title in bold)
print("\n\033[1mManifest misses:\033[0m")
for pid_data in manifest_misses:
    print(pid_data["source_file_path"])
    