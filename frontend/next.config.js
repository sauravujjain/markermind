/** @type {import('next').NextConfig} */
const nextConfig = {
  // 'standalone' bundles a self-contained server for Docker/GCP deployment.
  // - Local dev:  `npx next dev`
  // - Local prod: `npx next build && npx next start`  (works WITHOUT standalone)
  // - GCP/Docker: `npx next build && node .next/standalone/server.js` (needs standalone)
  // Enable standalone only for container builds (set STANDALONE=1 in CI/Dockerfile).
  ...(process.env.STANDALONE === '1' ? { output: 'standalone' } : {}),
  experimental: {
    serverActions: {
      allowedOrigins: ['localhost:3000', '0.0.0.0:3000', '127.0.0.1:3000'],
    },
  },
  compiler: {
    removeConsole: process.env.NODE_ENV === 'production' ? { exclude: ['error', 'warn'] } : false,
  },
  reactStrictMode: false,
}

module.exports = nextConfig
