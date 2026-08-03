FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Browser stays same-origin via /backend; Next rewrites proxy to Railway backend.
ARG API_PROXY_TARGET=https://dfi-backend-production.up.railway.app
ENV API_PROXY_TARGET=$API_PROXY_TARGET
ARG NEXT_PUBLIC_API_URL=/backend
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN mkdir -p public
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
ENV PORT=3002
ENV HOSTNAME=0.0.0.0
EXPOSE 3002
CMD ["node", "server.js"]
