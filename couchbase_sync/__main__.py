
import contextlib
import logging
from server import CouchbaseServer


SERVER_LIST = ["10.105.20.235", "10.250.20.241"]
SERVER_LOGIN = "clcache"
SERVER_PASSWORD = "clcache"


def main():
    logging.basicConfig(level=logging.INFO)
    servers = []
    for server in SERVER_LIST:
        server = CouchbaseServer(server, SERVER_LOGIN, SERVER_PASSWORD)
        servers.append(server)

    sync(servers[0], servers[1])
    sync(servers[1], servers[0])


def sync(from_server: CouchbaseServer, to_server: CouchbaseServer):
    try:
        # retreive objects and expiration times
        o1 = from_server.get_objects_id_by_expiration()
        o2 = to_server.get_objects_id_by_expiration()

        # find objects only in server 1
        only_in_server_1 = set(o1.keys()) - set(o2.keys())
        for object_id in only_in_server_1:
            try:
                # Log message with timestamp
                logging.info(f"Syncing object {object_id} from {from_server.host} to {to_server.host}")
                
                # fetch object from server 1
                if o := from_server.get_object(object_id):

                    # get manifest for objects only in server 1
                    if result := from_server.get_manifest_by_object_hash(object_id):
                        (manifest_id, manifest) = result

                        # store in server 2 (this will merge the manifests)
                        if to_server.set_manifest(manifest_id, manifest):

                            # Store object in server 2
                            to_server.set_object(object_id, o)
            except Exception as e:
                logging.error(e)
    except Exception as e:
        logging.error(e)


if __name__ == "__main__":
    main()
