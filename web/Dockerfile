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

# The only server block: replaces the image's default server on port 80. The image's main
# /etc/nginx/nginx.conf includes conf.d/*.conf inside its http {} block, which the http-level
# directives at the top of nginx.conf (resolver, map) rely on. Because this file differs from
# the packaged default, the entrypoint's IPv6 script leaves it unchanged.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# The web root holds the build output and nothing else: the image's stock index.html and
# nginx-branded 50x.html are removed first, so no page outside the SPA is ever served (the
# stock error page would also name the server software that `server_tokens off` hides).
RUN rm -rf /usr/share/nginx/html/*
COPY --from=build /app/dist /usr/share/nginx/html

# The port nginx.conf listens on. The base image's own EXPOSE 80 metadata is inherited and
# cannot be removed, but nothing listens on 80, and Compose publishes 8080 only.
EXPOSE 8080
