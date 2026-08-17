FROM node:22.19.0-bookworm-slim
WORKDIR /workspace
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
COPY packages/api-client/package.json packages/api-client/package.json
RUN npm ci
COPY apps/web apps/web
COPY packages/api-client packages/api-client
WORKDIR /workspace/apps/web
EXPOSE 3000
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
