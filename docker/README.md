# Build instructions using Docker

This directory contains a Dockerfile that can be used to build the project in a containerized environment.

## Building the Docker image

To run docker image, use:

```bash
cd <repo>
docker build -t clcache_build -f docker/Dockerfile docker
docker run -it --rm -v .:/src clcache_build
```