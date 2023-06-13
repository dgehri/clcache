1. docker swarm init
2. docker image build -t couchbase_sync:latest clcache_sync.Dockerfile
3. cd couchbase && docker image build -t couchbase:latest .
4. docker stack deploy -c docker-compose.yml clcache
5. docker service ls
6. docker service logs couchbase_sync_couchbase_sync -f