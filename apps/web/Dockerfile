FROM node:22.19.0-bookworm-slim
WORKDIR /workspace
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/package.json
COPY packages/api-client/package.json packages/api-client/package.json
RUN pnpm install --frozen-lockfile
COPY apps/web apps/web
COPY packages/api-client packages/api-client
WORKDIR /workspace/apps/web
EXPOSE 3000
CMD ["pnpm", "run", "dev", "--host", "0.0.0.0"]
