# -----------------------------------------------------------
#  [*] knfapp-assistant — production image
#
#  The AI support agent's protocol head, baked: source and
#  locked dependencies in the image, nothing mounted. Runs
#  read_only in compose — node needs no writable disk here.
# -----------------------------------------------------------

FROM node:24.16.0-alpine

WORKDIR /app

COPY app /app

RUN npm install --omit=dev

ENV PORT=3000
EXPOSE 3000

CMD ["node", "server.js"]
