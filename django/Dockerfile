############################################################
#  [*] Django backend — image build
#
#  Single image for dev and prod:
#    - DJANGO_DEBUG=True  → runserver (auto-reload; compose
#      will bind-mount ./django:/app over the baked copy)
#    - otherwise          → gunicorn
#
#  NOT wired into docker-compose yet — this image joins the
#  stack at cutover, together with the cron container.
############################################################

FROM python:3.13-slim

WORKDIR /app


# Django + configuration
RUN pip install --no-cache-dir                  \
    django==5.2.15                              \
    django-environ==0.13.0

# Auth + serving + the image re-encode gate
RUN pip install --no-cache-dir                  \
    bcrypt==4.2.1                               \
    pillow==12.3.0                              \
    gunicorn==26.0.0

# The scrapers and the Expo push sender
RUN pip install --no-cache-dir                  \
    requests==2.32.3                            \
    beautifulsoup4==4.13.4                      \
    lxml==5.4.0

# The chat socket layer — the mobile client's socket.io
# protocol version rides on these pins
RUN pip install --no-cache-dir                  \
    python-socketio==5.16.1                     \
    python-engineio==4.13.1

# The wayfind stitch worker's composite math
RUN pip install --no-cache-dir                  \
    numpy==2.5.2


# Copy source code
COPY . /app


# Run
#
# gunicorn logs every request to stdout; the first column is
# the client IP Caddy forwarded (X-Forwarded-For) — %(h)s
# would only ever show the Caddy container. The control
# socket is off: the root filesystem is read-only.
#
# ONE gthread worker, many threads — NOT a worker pool: the
# chat presence table, the socket.io rooms and the per-event
# rate limiter are in-process state (chat/socket.py). The
# threads carry the REST requests and the polling transport
# side by side. A DJANGO_DEBUG runserver serves REST only
# (no socket layer — see knfapp/wsgi.py).
EXPOSE 8000
CMD sh -c "\
    if [ \"$DJANGO_DEBUG\" = 'True' ]; then \
        python3 manage.py runserver 0.0.0.0:8000; \
    else \
        python3 manage.py migrate --noinput && \
        gunicorn knfapp.wsgi:application --bind 0.0.0.0:8000 \
            --workers 1 --threads 24 --worker-class gthread \
            --access-logfile - \
            --access-logformat '%({x-forwarded-for}i)s %(t)s \"%(r)s\" %(s)s %(b)s %(M)sms' \
            --no-control-socket; \
    fi"
