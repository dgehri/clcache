# Install Python
RUN C:\python-installer.exe /quiet InstallAllUsers=1 PrependPath=1

# Remove the installer
RUN del C:\python-installer.exe

# Clone your project (adjust the repository URL as needed)
RUN git clone https://github.com/dgehri/clcache.git C:/app

WORKDIR C:/app

# Install dependencies using pipenv and Pipfile in the cloned repo
ENV PIPENV_PIPFILE=C:/app/clcache/Pipfile

# Install pipenv and project dependencies
RUN pip install pipenv
RUN pipenv install

# Compile using Nuitka
RUN pipenv run python -m nuitka \
    --standalone \
    --plugin-enable=pylint-warnings \
    --no-deployment-flag=self-execution \
    --python-flag="-O" \
    --mingw64 \
    .\clcache
