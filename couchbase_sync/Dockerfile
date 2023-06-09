# Use alpine
FROM python:3.10.0-alpine

# Install pipenv
RUN pip3 install pipenv

# Install git
RUN apk add --no-cache git

# Create /opt/couchbase_sync
RUN mkdir -p /opt/couchbase_sync
WORKDIR /opt/couchbase_sync

# Clone couchbase_sync from https://github.com/dgehri/clcache.git
RUN git clone https://github.com/dgehri/clcache.git

# Change into clcache/couchbase_sync
WORKDIR /opt/couchbase_sync/clcache/couchbase_sync

# Install dependencies
# RUN pipenv install
