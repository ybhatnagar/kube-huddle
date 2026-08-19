# UI image — the static bundle on unprivileged nginx (runs as non-root, listens :8080).
# In-cluster, the Helm chart mounts a config that also proxies /api/ to the engine so
# the browser talks to a single origin; standalone it just serves the static files.
FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY index.html /usr/share/nginx/html/
COPY nginx.default.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
