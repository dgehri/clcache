FROM couchbase:community

# Install bc
RUN apt-get update && apt-get install -y bc netcat

COPY configure-couchbase.sh /opt/couchbase

RUN chmod +x /opt/couchbase/configure-couchbase.sh

CMD ["/opt/couchbase/configure-couchbase.sh"]

