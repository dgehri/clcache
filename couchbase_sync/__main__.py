
import itertools
import logging
import os
import time
from server import CouchbaseServer
from tendo import singleton
import sys
import docker

lockfile = "/tmp/couchbase_sync.lock"
file_handle = None

SERVER_LIST = ["10.105.20.235", "10.250.20.241"]
SERVER_LOGIN = "clcache"
SERVER_PASSWORD = "clcache"


def main():
    # Log with timestamp
    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    servers = []
    try:
        # Determine all nodes in the docker swarm
        client = docker.DockerClient(base_url='unix://var/run/docker.sock')
        nodes = client.nodes.list()
        for node in nodes:
            server_ip = node.attrs['Status']['Addr']
            logging.info(f"Adding node {server_ip}")
            server = CouchbaseServer(server_ip, SERVER_LOGIN, SERVER_PASSWORD)
            ignored_object_ids = set()
            servers.append((server, ignored_object_ids))
    except Exception as e:
        logging.error(e)
        sys.exit(1)

    server_pairs = [
        (servers[i], servers[j])
        for i, j in itertools.product(range(len(servers)), range(len(servers)))
        if i != j
    ]
    
    while True:
        for pair in server_pairs:
            logging.info(f"Syncing {pair[0][0].host} to {pair[1][0].host}")
            sync(pair[0], pair[1][0])
            time.sleep(30)


def sync(src: tuple[CouchbaseServer, set[str]], dst_server: CouchbaseServer):
    src_server, src_ignored_object_ids = src
    sync_source = src_server.host
    
    try:
        # retreive objects
        o1 = src_server.get_unsynced_object_ids()
        o2 = dst_server.get_unsynced_object_ids()

        # find objects only in server 1
        only_in_server_1 = (o1 - o2) - src_ignored_object_ids
        
        for object_id in only_in_server_1:
            try:
                # fetch object from server 1
                if o := src_server.get_object(object_id):

                    # get manifest for objects only in server 1
                    if result := src_server.get_manifest_by_object_hash(object_id):
                        (manifest_id, manifest) = result

                        # store in server 2 (this will merge the manifests)
                        if dst_server.set_manifest(manifest_id, manifest):

                            # Store object in server 2
                            if dst_server.set_object(object_id, o, sync_source):
                                logging.info(f"Synced object {object_id} from {src_server.host} to {dst_server.host}")
                        else:
                            logging.error(
                                f"Failed to store manifest for object {object_id} from {src_server.host}")
                    else:
                        src_ignored_object_ids.add(object_id)
                        logging.error(
                            f"Failed to fetch manifest for object {object_id} from {src_server.host}")
                        
                else:
                    logging.error(
                        f"Failed to fetch object {object_id} from {src_server.host}")
            except Exception as e:
                logging.error(e)
    except Exception as e:
        logging.error(e)


if __name__ == "__main__":
    # Create singleton instance
    try:
        me = singleton.SingleInstance()  # will sys.exit(-1) if other instance is running
        main()
    except singleton.SingleInstanceException:
        sys.exit("Another instance of the app is already running, quitting.")
