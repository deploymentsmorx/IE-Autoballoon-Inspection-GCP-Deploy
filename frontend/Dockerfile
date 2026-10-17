FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_API_URL=""
ARG VITE_ADMIN_PORTAL_ONLY="false"
ENV VITE_API_URL=$VITE_API_URL
ENV VITE_ADMIN_PORTAL_ONLY=$VITE_ADMIN_PORTAL_ONLY
RUN npm run build && if [ "$VITE_ADMIN_PORTAL_ONLY" = "true" ]; then \
    sed -i 's|<title>.*</title>|<title>Cost Sheet Admin Portal</title>|' dist/index.html; \
  fi

FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
