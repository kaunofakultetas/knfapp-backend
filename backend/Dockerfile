############################################################
#  [*] backend/Dockerfile — the knfapp-backend image
#
#  Two stages so the runtime image ships no C toolchain:
#
#    builder  — gcc/musl/libffi/libxml headers, pip installs
#               every wheel into /install
#    runtime  — clean base + /install copied over /usr/local,
#               only the shared libs lxml links against
#
#  The base tag is pinned to an exact patch (a floating
#  python:3.13-alpine silently changed the interpreter under
#  the app on every rebuild). Bump it deliberately; pin it by
#  digest on the deploy host once pulled:
#    docker image inspect --format='{{index .RepoDigests 0}}' \
#      python:3.13.7-alpine
#
#  Runs as 1000:1000 with read_only: true — everything
#  writable is a mount (/data) or the tmpfs (/tmp), which is
#  why nothing here writes outside /app.
############################################################

FROM python:3.13.7-alpine AS builder

RUN apk add --no-cache gcc musl-dev libffi-dev libxml2-dev libxslt-dev

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.13.7-alpine

# Shared libs only — lxml links against them at import time;
# the headers and the compiler stay behind in the builder
RUN apk add --no-cache libxml2 libxslt

WORKDIR /app

COPY --from=builder /install /usr/local

COPY . .

RUN adduser -D -u 1000 appuser
USER 1000:1000

# Unbuffered: logging.basicConfig writes to stdout and Docker
# must see each line as it happens, not one page at a time
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Baked in rather than left to compose: the live compose file
# is gitignored, so a healthcheck defined here is the one that
# actually reaches production. busybox wget ships in alpine
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "main.py", "--http"]
