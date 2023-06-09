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
ADD https://api.github.com/repos/dgehri/clcache/git/refs/heads/master version.json
RUN git clone https://github.com/dgehri/clcache.git

# Change into clcache/couchbase_sync
WORKDIR /opt/couchbase_sync/clcache/couchbase_sync

# Install dependencies (couchbase)
RUN pipenv install --verbose

# Add the sync script to the package path
ENV PYTHONPATH=/opt/couchbase_sync/clcache/couchbase_sync

# Create a cronjob to run the sync every 5 minutes (but only if the previous one is finished)
RUN echo "*/5 * * * * /usr/local/bin/pipenv run python3 -m couchbase_sync" > /etc/crontabs/root

# Reload the cron daemon
RUN crond -c /etc/crontabs

# Run tail to keep the container running
RUN tail -f /dev/null
