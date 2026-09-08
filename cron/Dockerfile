############################################################
#  [*] knfapp cron — the scheduled scrape and sweep ticks
#
#  Tiny scheduler container: busybox crond + docker CLI.
#  Runs `docker exec` into the django container on schedule
#  (see ./crontab) — scheduling lives OUTSIDE the web
#  process on purpose: a container that only serves
#  requests never scrapes, and a scrape survives a web
#  deploy. Needs /var/run/docker.sock mounted and
#  network_mode: none (it talks to the socket, not the
#  network).
#
#  The crontab is baked in at build time so it is owned by
#  root — busybox crond silently ignores crontab files with
#  other owners.
############################################################

FROM docker:29-cli

COPY crontab /etc/crontabs/root
RUN chmod 600 /etc/crontabs/root

CMD ["crond", "-f", "-d", "8"]
