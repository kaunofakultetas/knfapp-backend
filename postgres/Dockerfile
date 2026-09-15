FROM postgres:17.2-alpine

# Install pgvector
RUN apk add --no-cache --virtual .build-deps git build-base clang19 llvm19 \
    && git clone --branch v0.8.0 --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && make -C /tmp/pgvector \
    && make -C /tmp/pgvector install \
    && rm -rf /tmp/pgvector \
    && apk del .build-deps

    