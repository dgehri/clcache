# Deploy Couchbase and Couchbase-Sync

1. Create swarm: `docker swarm init`
2. Create secret: `echo <password> | docker secret create couchbase_admin_password`
3. Build images:
    3.1. Couchbase
        - `pushd couchbase`
        - `docker image build -t couchbase_clcache:latest .`
        - `docker tag couchbase_clcache:latest us-docker.pkg.dev/inr-ci/inr-docker/couchbase_clcache:latest`
        - `docker image push us-docker.pkg.dev/inr-ci/inr-docker/couchbase_clcache:latest`
        - `popd`
    3.2. Couchbase-Sync
        - `docker image build -t couchbase_sync:latest -f clcache_sync.Dockerfile .`
        - `docker tag couchbase_sync:latest us-docker.pkg.dev/inr-ci/inr-docker/couchbase_sync:latest`
        - `docker image push us-docker.pkg.dev/inr-ci/inr-docker/couchbase_sync:latest`
4. Create and deploy stack: `docker stack deploy -c docker-compose.yml clcache`
5. Verify stack: `docker stack ps clcache`
6. Read logs: 
   - `docker container ls` then `docker container logs <container_id>`
   - `docker service logs -f clcache_sync`
  
# Update Couchbase and Couchbase-Sync

1. Update images (see above)
2. Update stack: `docker service update --image us-docker.pkg.dev/inr-ci/inr-docker/couchbase_sync:latest clcache_couchbase_sync`


```bash
docker image build -t couchbase_sync:latest -f clcache_sync.Dockerfile .
docker tag couchbase_sync:latest us-docker.pkg.dev/inr-ci/inr-docker/couchbase_sync:latest
docker image push us-docker.pkg.dev/inr-ci/inr-docker/couchbase_sync:latest
docker service update --image us-docker.pkg.dev/inr-ci/inr-docker/couchbase_sync:latest clcache_couchbase_sync
```

