import itertools
import logging
import concurrent.futures
import os
import signal
import time
from server import CouchbaseServer
from tendo import singleton
import sys
import traceback

lockfile = "/tmp/couchbase_sync.lock"
file_handle = None

SERVER_LOGIN = "clcache"
SERVER_PASSWORD = "clcache"


def main():
    # Log with timestamp
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(thread)d] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    servers = []
    try:
        # Extract server IPs from "NODES" environment variable (comma separated list)
        for server_ip in os.environ["NODES"].split(","):
            logging.info(f"Adding node {server_ip}")
            server = CouchbaseServer(server_ip, SERVER_LOGIN, SERVER_PASSWORD)
            servers.append(server)
    except Exception as e:
        logging.error(e)
        sys.exit(1)

    server_pairs = [
        (servers[i], servers[j])
        for i, j in itertools.product(range(len(servers)), range(len(servers)))
        if i != j
    ]

    killer = GracefulKiller()

    while True:
        for pair in server_pairs:
            logging.info(f"Syncing {pair[0].host} to {pair[1].host}")
            sync_count = sync(*pair, killer)
            
            if killer.kill_now:
                logging.info("Killing process")
                sys.exit(0)

            if sync_count == 0:
                for _ in range(10):
                    if killer.kill_now:
                        logging.info("Killing process")
                        sys.exit(0)
                    time.sleep(1)


def sync(src_server: CouchbaseServer, dst_server: CouchbaseServer, killer) -> int:
    sync_source = src_server.host
    sync_dest = dst_server.host
    sync_count = 0
    fail_count = 0

    try:
        # retrieve objects
        o1 = src_server.get_unsynced_object_ids(not_from=sync_dest)
        o2 = dst_server.get_unsynced_object_ids()

        # find objects only in server 1
        only_in_server_1 = o1 - o2

        logging.info(f"Found {len(only_in_server_1)} objects to sync")

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(
                    sync_object, object_id, src_server, dst_server, killer
                ): object_id
                for object_id in only_in_server_1
            }
            for future in concurrent.futures.as_completed(futures):
                if success := future.result():
                    if success:
                        sync_count += 1
                    else:
                        fail_count += 1

    except Exception as e:
        logging.error(f"{e}: {traceback.format_exc()}")

    logging.info(
        f"Synced {sync_count} objects from {sync_source} to {sync_dest} ({fail_count} failed)"
    )
    
    return sync_count


def sync_object(
    object_id: str, src_server: CouchbaseServer, dst_server: CouchbaseServer, killer
) -> bool:
    if killer.kill_now:
        logging.info("Killing process")
        sys.exit(0)

    sync_source = src_server.host
    result = False

    try:
        if not (o := src_server.get_object(object_id)):
            raise RuntimeError(
                f"Failed to fetch object {object_id} from {src_server.host}"
            )

        # get manifest for objects only in server 1
        if not (result := src_server.get_manifest_by_object_hash(object_id)):
            # delete object
            src_server.delete_object(object_id)
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
            result = True

    except Exception as e:
        logging.error(e)

    return result


class GracefulKiller:
    kill_now = False

    def __init__(self):
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def exit_gracefully(self, signum, frame):
        self.kill_now = True


if __name__ == "__main__":
    # Create singleton instance
    try:
        me = (
            singleton.SingleInstance()
        )  # will sys.exit(-1) if other instance is running
        main()
    except singleton.SingleInstanceException:
        sys.exit("Another instance of the app is already running, quitting.")
