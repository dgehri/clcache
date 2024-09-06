# From https://github.com/webcomics/pywine/blob/main/Dockerfile
FROM tobix/pywine:3.11

# Also install winetricks
RUN dpkg --add-architecture i386 && \
    sed -i 's/^Components: main$/& contrib non-free/' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends winetricks cabextract expect patch wget && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install required Python modules
COPY requirements.txt /opt/clcache/requirements.txt
RUN wine cmd /c setx PATH "C:\\Python\\Scripts;%PATH%" && \
    wine pip install --use-pep517 -r /opt/clcache/requirements.txt

# Fetch Dependency Walker
ENV DEPENDS_DIR="/opt/wineprefix/drive_c/users/root/AppData/Local/Nuitka/Nuitka/Cache/downloads/depends/x86_64"
WORKDIR ${DEPENDS_DIR}
RUN wget https://www.dependencywalker.com/depends22_x86.zip && \
    unzip depends22_x86.zip

# Patch the Nuitka DependsExe.py to make it work with Wine
COPY DependsExe.patch /opt/clcache/DependsExe.patch
RUN find /opt/wineprefix/drive_c -name DependsExe.py -exec patch -d $(dirname {}) -p0 < /opt/clcache/DependsExe.patch \;

# Compile a dummy Python program to force Nuitka to download the MinGW compiler
#
# Note that the "cmd /c" is necessary for correct stdout/stderr redirection
WORKDIR /tmp/init
RUN echo "print('Hello, world')" > hello.py && \
    wine \
        python -m nuitka \
            --mingw64 \
            --assume-yes-for-downloads \
            --remove-output \
            --disable-ccache \
            --run \
            hello.py; wineserver -w && \
    rm -rf /tmp/init

COPY entrypoint.sh /opt/clcache/entrypoint.sh
RUN chmod +x /opt/clcache/entrypoint.sh

WORKDIR /opt/clcache
ENTRYPOINT ["/opt/clcache/entrypoint.sh"]
