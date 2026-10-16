FROM node:24-alpine AS build
WORKDIR /src
COPY console/package.json console/package-lock.json ./console/
RUN cd console && npm ci --no-audit --no-fund
COPY api/openapi.yaml ./api/openapi.yaml
COPY console ./console
RUN cd console && npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
ENV API_UPSTREAM=http://api:8000
COPY console/deploy/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /src/console/dist /usr/share/nginx/html
EXPOSE 8080
