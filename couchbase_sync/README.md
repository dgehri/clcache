1. docker swarm init
2. docker image build -t couchbase_sync:lastest .
3. docker stack deploy -c docker-compose.yml couchbase_sync
4. docker service ls