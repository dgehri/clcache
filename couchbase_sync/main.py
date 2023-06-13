import itertools
import logging
import os
import time
from server import CouchbaseServer
from tendo import singleton
import sys

lockfile = "/tmp/couchbase_sync.lock"
file_handle = None

SERVER_LOGIN = "clcache"
SERVER_PASSWORD = "clcache"


def main():
    # Log with timestamp
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    servers = []
    try:
        # Extract server IPs from "NODES" environment variable (comma separated list)
        for server_ip in os.environ["NODES"].split(","):
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
    sync_dest = dst_server.host
    sync_count = 0
    fail_count = 0

    try:
        # retreive objects
        o1 = src_server.get_unsynced_object_ids(sync_dest)
        o2 = dst_server.get_unsynced_object_ids(sync_source)

        # find objects only in server 1
        only_in_server_1 = (o1 - o2) - src_ignored_object_ids
        
        logging.info(f"Found {len(only_in_server_1)} objects to sync")

        for object_id in only_in_server_1:
            try:
                if not (o := src_server.get_object(object_id)):
                    raise RuntimeError(
                        f"Failed to fetch object {object_id} from {src_server.host}"
                    )

                # get manifest for objects only in server 1
                if not (result := src_server.get_manifest_by_object_hash(object_id)):
                    src_ignored_object_ids.add(object_id)
                    raise RuntimeError(
                        f"Failed to fetch manifest for object {object_id} from {src_server.host}"
                    )

                (manifest_id, manifest) = result

                # store in server 2 (this will merge the manifests)
                if not dst_server.set_manifest(manifest_id, manifest):
                    raise RuntimeError(
                        f"Failed to store manifest for object {object_id} from {src_server.host}"
                    )

                # Store object in server 2
                if dst_server.set_object(object_id, o, sync_source):
                    logging.info(
                        f"Synced object {object_id} from {src_server.host} to {dst_server.host}"
                    )
                    sync_count += 1

            except Exception as e:
                logging.error(e)
                fail_count += 1

    except Exception as e:
        logging.error(e)

    logging.info(
        f"Synced {sync_count} objects from {sync_source} to {sync_dest} ({fail_count} failed)"
    )


if __name__ == "__main__":
    # Create singleton instance
    try:
        me = (
            singleton.SingleInstance()
        )  # will sys.exit(-1) if other instance is running
        main()
    except singleton.SingleInstanceException:
        sys.exit("Another instance of the app is already running, quitting.")
