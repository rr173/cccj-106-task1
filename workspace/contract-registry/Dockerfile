FROM node:20-alpine

ENV NODE_ENV=production \
    PORT=8000 \
    DATA_FILE=/data/registry.json \
    MAX_EXEMPTION_DAYS=30

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY src ./src

# 数据目录挂载点；以非 root 用户运行
RUN mkdir -p /data && chown -R node:node /data /app
USER node
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD wget -qO- http://127.0.0.1:8000/health || exit 1

CMD ["node", "src/server.js"]
