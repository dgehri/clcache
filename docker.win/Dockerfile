FROM mcr.microsoft.com/windows-cssc/python:3.11-servercore-ltsc2022

# Install Python modules
COPY requirements.txt C:/clcache/requirements.txt
RUN python -m pip install --use-pep517 -r C:/clcache/requirements.txt

ENV LOCALAPPDATA="C:/Users/ContainerAdministrator/AppData/Local"
ENV CACHE_DIR="${LOCALAPPDATA}/Nuitka/Nuitka/Cache"

# Change to PowerShell
SHELL ["powershell", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue';"]

# ADD https://github.com/brechtsanders/winlibs_mingw/releases/download/13.2.0-16.0.6-11.0.1-msvcrt-r1/winlibs-x86_64-posix-seh-gcc-13.2.0-llvm-16.0.6-mingw-w64msvcrt-11.0.1-r1.zip \
#     ${CACHE_DIR}/downloads/gcc/x86_64/13.2.0-16.0.6-11.0.1-msvcrt-r1/winlibs-x86_64-posix-seh-gcc-13.2.0-llvm-16.0.6-mingw-w64msvcrt-11.0.1-r1.zip
WORKDIR ${CACHE_DIR}/downloads/gcc/x86_64/13.2.0-16.0.6-11.0.1-msvcrt-r1/
RUN ["curl", "-L", "-O", "https://github.com/brechtsanders/winlibs_mingw/releases/download/13.2.0-16.0.6-11.0.1-msvcrt-r1/winlibs-x86_64-posix-seh-gcc-13.2.0-llvm-16.0.6-mingw-w64msvcrt-11.0.1-r1.zip"]
        
    
# Compile a dummy Python program to trigger Nuitka setup
WORKDIR C:/tmp/init
RUN ["powershell",  "-Command", "Set-Content -Path hello.py -Value 'print(''Hello, world'')'"]
RUN python -m nuitka --mingw64 --assume-yes-for-downloads --remove-output --disable-ccache --run --standalone hello.py

# Add entrypoint script
COPY entrypoint.ps1 C:/clcache/entrypoint.ps1

WORKDIR C:/src
ENTRYPOINT ["powershell", "C:/clcache/entrypoint.ps1"]
