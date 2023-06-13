# Use alpine
FROM python:3.10.0-alpine

# Install build dependencies
RUN apk add --no-cache gcc g++ make libffi-dev openssl-dev build-base git

# Install pipenv
RUN pip3 install pipenv

# Create /opt/couchbase_sync
RUN mkdir -p /opt/couchbase_sync
WORKDIR /opt/couchbase_sync

# Clone couchbase_sync from https://github.com/dgehri/clcache.git
ADD https://api.github.com/repos/dgehri/clcache/git/refs/tags/couchbase_sync_base version_base.json
RUN git clone https://github.com/dgehri/clcache.git

# Change into clcache/couchbase_sync
WORKDIR /opt/couchbase_sync/clcache/couchbase_sync

# Install dependencies (couchbase)
RUN pipenv install --verbose

# Update to tag couchbase_sync
ADD https://api.github.com/repos/dgehri/clcache/git/refs/heads/master version.json
RUN git fetch --all --tags --prune --force && git checkout master --force && git pull

# Re-sync pipenv
RUN pipenv sync --verbose

# When container is run, execute the following command
# /usr/local/bin/pipenv run python3 -m /opt/couchbase_sync/clcache/couchbase_sync/__main__.py
ENTRYPOINT ["/usr/local/bin/pipenv", "run", "python3", "/opt/couchbase_sync/clcache/couchbase_sync/main.py"]
