# SmorX AutoBalloon — web UI (static React build + nginx)
# Build: docker build --build-arg VITE_API_URL=https://api.example.com -t autoballoon-web .

FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_API_URL=http://127.0.0.1:9686
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
