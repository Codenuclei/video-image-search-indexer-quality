FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Browser stays same-origin via /api/proxy; App Router route proxies to Railway backend.
ARG API_PROXY_TARGET=https://dfi-backend-production.up.railway.app
ENV API_PROXY_TARGET=$API_PROXY_TARGET
ARG NEXT_PUBLIC_API_URL=/api/proxy
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN mkdir -p public
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
# Runtime proxy target (builder ENV is not inherited across stages).
ARG API_PROXY_TARGET=https://dfi-backend-production.up.railway.app
ENV API_PROXY_TARGET=$API_PROXY_TARGET
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
ENV PORT=3002
ENV HOSTNAME=0.0.0.0
EXPOSE 3002
CMD ["node", "server.js"]
