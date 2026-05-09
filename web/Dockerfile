FROM node:20-alpine AS builder
WORKDIR /repo

COPY pi/categories.json /repo/pi/categories.json

WORKDIR /repo/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY web/ ./
RUN npm run build


FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

COPY --from=builder /repo/web/.next/standalone ./
COPY --from=builder /repo/web/.next/static ./.next/static
COPY --from=builder /repo/web/public ./public

EXPOSE 3000
CMD ["node", "server.js"]
