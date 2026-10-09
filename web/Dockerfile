# Ads Action Engine web image: the built SPA, served by nginx with the /api reverse proxy.
#
# Build context: ./web (root docker-compose.yml, service `web`). Two stages:
#   build    node:24.21.0-slim  `npm ci` from the committed lockfile, then `npm run build`
#                               (vite build), which emits /app/dist: index.html and assets/
#   runtime  nginx:1.30.5       the contents of dist/ as the web root, and nginx.conf as the
#                               only server: listens on 8080, proxies /api/ to http://api:8000,
#                               falls back to index.html for SPA routes, sets the security
#                               headers
#
# Both base tags are exact, so a rebuild cannot change the build toolchain or the runtime
# silently.
#
# Runtime user: uid/gid 101, the base image's own `nginx` user. The entrypoint and every nginx
# process, the master included, run as it; nothing runs as root. Port 8080 is above 1024, so
# binding it needs no privilege. The runtime user writes only the pid file, /tmp/nginx.pid, and
# nginx's temporary-file directories under /var/cache/nginx (client_temp for request bodies,
# proxy_temp for API responses larger than the proxy buffers). /etc/nginx and the web root stay
# root-owned and read-only to it.
#
# Secrets and configuration: none. No ARG or ENV carries a value, and no .env* file is copied.
# The bundle reads no build-time configuration: it calls the relative path /api/v1, and every
# window length, preset and threshold comes from GET /api/v1/config at run time.
#
# Health: no HEALTHCHECK. Compose owns orchestration and defines health on the api service only;
# this image keeps nginx's default entrypoint and CMD (nginx -g 'daemon off;').

# -------------------------------------------------------------------------------------------
# Build stage
# -------------------------------------------------------------------------------------------
FROM node:24.21.0-slim AS build

WORKDIR /app

# Dependency layer, cached separately from the source: it is rebuilt only when the manifest or
# the lockfile changes. `npm ci` installs exactly what package-lock.json records and fails when
# the lockfile is out of sync with package.json, which is intended; the lockfile is never
# rewritten in the image.
#
# NODE_ENV is deliberately left unset: Vite, @vitejs/plugin-react and Vitest (vite.config.ts
# imports `vitest/config`) are devDependencies, and the build needs them. @playwright/test
# downloads no browsers on install, and nothing here runs `playwright install`.
# --no-audit, --no-fund and --no-update-notifier skip the advisory, funding and npm-version
# requests, which the build does not use. The npm cache is a build-time cache mount, so it
# never lands in a layer.
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
  npm ci --no-audit --no-fund --no-update-notifier

# Source layer. Explicit COPYs only, never `COPY . .`: a local node_modules, dist, coverage,
# Playwright report or .env in web/ can never reach the image, with no .dockerignore to
# maintain. These are the only inputs `vite build` reads: its config, the TypeScript options it
# applies to JSX, the entry document and the application source. Lint configs, tests and the
# Playwright config are not part of the build.
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src

RUN npm run build

# -------------------------------------------------------------------------------------------
# Runtime stage
# -------------------------------------------------------------------------------------------
FROM nginx:1.30.5

LABEL org.opencontainers.image.title="ads-engine-web" \
  org.opencontainers.image.description="Ads Action Engine SPA served by nginx, with /api proxied to the API"

# Non-root runtime. The base image's /etc/nginx/nginx.conf is written for a root master that
# drops its workers to `user nginx` and writes its pid file to root-owned /run. This edit
# deletes the `user` directive (a non-root master cannot switch users, and nginx warns when the
# directive is set), moves the pid file to /tmp/nginx.pid (the runtime user creates it there
# and removes it on exit), and gives /var/cache/nginx, where nginx creates its temporary-file
# directories at start, to the runtime user. Every edit is checked, as is the `nginx` user's
# uid and gid: if the base image's lines ever change, the build fails instead of shipping a
# configuration that only root can run.
RUN sed -i \
    -e '/^user  nginx;$/d' \
    -e 's|^pid        /run/nginx\.pid;$|pid        /tmp/nginx.pid;|' \
    /etc/nginx/nginx.conf \
  && ! grep -Eq '^[[:space:]]*user[[:space:]]' /etc/nginx/nginx.conf \
  && grep -Eq '^pid[[:space:]]+/tmp/nginx\.pid;$' /etc/nginx/nginx.conf \
  && [ "$(id -u nginx):$(id -g nginx)" = "101:101" ] \
  && chown -R 101:101 /var/cache/nginx

# The only server block: replaces the image's default server on port 80. The image's main
# /etc/nginx/nginx.conf includes conf.d/*.conf inside its http {} block, which the http-level
# directives at the top of nginx.conf (resolver, upstream, map) rely on. The file is
# root-owned and differs from the packaged default, so the entrypoint's IPv6 script leaves it
# unchanged; the entrypoint's other scripts act only on environment variables this image does
# not set and on templates it does not ship. --chmod=0644 makes it readable to the runtime
# user whatever mode the checkout gives the file (a 0600 copy would stop nginx from starting)
# and keeps it writable by root only.
COPY --chmod=0644 nginx.conf /etc/nginx/conf.d/default.conf

# The web root holds the build output and nothing else: the image's stock index.html and
# nginx-branded 50x.html are removed first, so no page outside the SPA is ever served (the
# stock error page would also name the server software that `server_tokens off` hides).
RUN rm -rf /usr/share/nginx/html/*
COPY --from=build /app/dist /usr/share/nginx/html

# The base image's `nginx` user (uid/gid 101, checked above), given numerically so a
# runAsNonRoot policy can verify it without reading /etc/passwd. It runs the entrypoint and
# every nginx process; the configuration and the web root above stay root-owned.
USER 101:101

# The port nginx.conf listens on. The base image's own EXPOSE 80 metadata is inherited and
# cannot be removed, but nothing listens on 80, and Compose publishes 8080 only.
EXPOSE 8080
