# Build instructions using Docker

This directory contains a Dockerfile that can be used to build the project in a containerized environment.

## Building the Docker image

To run docker image, use:

(the parent folder needs to be mounted into /src)

```bash
cd <repo>
docker build -t clcache_build -f docker/Dockerfile docker
docker run --rm -v .:/src clcache_build
```