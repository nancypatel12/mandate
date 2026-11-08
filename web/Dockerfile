FROM node:22-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# nginx serves the console and is its trust boundary: see nginx.conf.template.
FROM nginx:1.29-alpine
COPY --from=build /web/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
