# From https://github.com/webcomics/pywine/blob/main/Dockerfile
FROM tobix/pywine:3.10

# Also install winetricks
RUN dpkg --add-architecture i386 && \
    sed -i 's/^Components: main$/& contrib non-free/' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends winetricks cabextract && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install MFC42
RUN xvfb-run sh -c "winetricks -q mfc42; wineserver -w"

# Wine: Add C:\Python\Scripts to PATH
ENV PATH="C:\\Python\\Scripts;${PATH}"

# Install required Python modules
RUN wine cmd /c setx PATH "C:\\Python\\Scripts;%PATH%" && \
    wine pip install --use-pep517 \
    atomicwrites \
    conan~=1.0 \
    lz4 \
    nuitka \
    scandir

ENV DEPENDS_DIR="/opt/wineprefix/drive_c/users/root/AppData/Local/Nuitka/Nuitka/Cache/downloads/depends/x86_64"
ADD https://www.dependencywalker.com/depends22_x86.zip ${DEPENDS_DIR}/depends22_x86.zip
RUN unzip ${DEPENDS_DIR}/depends22_x86.zip -d ${DEPENDS_DIR}

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
