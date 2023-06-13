#!/bin/bash

set -m

# Read COUCHBASE_ADMIN_PASSWORD from /run/secrets/couchbase_admin_password
COUCHBASE_ADMIN_PASSWORD=$(cat /run/secrets/couchbase_admin_password)

/entrypoint.sh couchbase-server &

echo "Waiting for Couchbase Server to be available..."
until $(curl --output /dev/null --silent --head --fail http://127.0.0.1:8091); do
    sleep 1
done
echo "Couchbase Server is available, configuring it..."

# Get total memory and calculate 75% of it
TOTAL_MEMORY=$(free -m | awk '/^Mem:/{print $2}')
DATA_MEMORY=$(echo "scale=0; (${TOTAL_MEMORY} * 0.75)/1" | bc -l)
# Bucket gets 5% less than the calculated value
BUCKET_RAMSIZE=$(echo "scale=0; (${DATA_MEMORY} * 0.95)/1" | bc -l)

# Setup initial cluster/ Initialize Node
couchbase-cli cluster-init -c 127.0.0.1 --cluster-username Administrator --cluster-password "${COUCHBASE_ADMIN_PASSWORD}" \
    --services data,index,query,fts --cluster-ramsize ${DATA_MEMORY} --cluster-index-ramsize 512 --cluster-fts-ramsize 512


# Common args
CONNECTION="-c 127.0.0.1 --username Administrator --password ${COUCHBASE_ADMIN_PASSWORD}"

# Create the buckets
couchbase-cli bucket-create ${CONNECTION} --bucket-type couchbase --bucket-ramsize ${BUCKET_RAMSIZE} --bucket clcache

# Create manifests collection
couchbase-cli collection-manage ${CONNECTION} --bucket clcache --create-collection _default.manifests

# Create objects collection
couchbase-cli collection-manage ${CONNECTION} --bucket clcache --create-collection _default.objects

# Create objects_data collection
couchbase-cli collection-manage ${CONNECTION} --bucket clcache --create-collection _default.objects_data

# Create user clcache / clcache w/o admin rights
couchbase-cli user-manage ${CONNECTION} \
    --set --rbac-username clcache --rbac-password clcache --rbac-name clcache --roles bucket_full_access[clcache] \
    --auth-domain local
    
# Ensure N1QL service is up and running before creating indices
CONNECTION="-u Administrator -p ${COUCHBASE_ADMIN_PASSWORD} -e http://127.0.0.1:8091"

until nc -z 127.0.0.1 8093; do
  >&2 echo "Couchbase N1QL service is unavailable - sleeping"
  sleep 1
done

until $(cbq ${CONNECTION} --exit-on-error --script="SELECT 1" > /dev/null 2>&1); do
    sleep 1
done

# Create indices:
#  - idx_entries_objectHash: CREATE INDEX `idx_entries_objectHash` ON `clcache`.`_default`.`manifests`((distinct (array (`v`.`objectHash`) for `v` in `entries` end)))
#  - idx_expiration_sync_source: CREATE INDEX `idx_expiration_sync_source` ON `clcache`.`_default`.`objects`((meta().`expiration`),`sync_source`) WHERE (0 < (meta().`expiration`))
#  - #primary: CREATE PRIMARY INDEX `#primary` ON `clcache`.`_default`.`objects`
cbq ${CONNECTION} --script="CREATE INDEX \`idx_entries_objectHash\` ON \`clcache\`.\`_default\`.\`manifests\`((distinct (array (\`v\`.\`objectHash\`) for \`v\` in \`entries\` end)));"
cbq ${CONNECTION} --script="CREATE INDEX \`idx_expiration_sync_source\` ON \`clcache\`.\`_default\`.\`objects\`((meta().\`expiration\`),\`sync_source\`) WHERE (0 < (meta().\`expiration\`));"
cbq ${CONNECTION} --script="CREATE PRIMARY INDEX \`#primary\` ON \`clcache\`.\`_default\`.\`objects\`;"

fg 1
