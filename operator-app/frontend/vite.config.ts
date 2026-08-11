// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import { defineConfig } from 'vite';

const frontendRoot = new URL('.', import.meta.url).pathname;

export default defineConfig({
  root: frontendRoot,
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    strictPort: true,
    proxy: {
      '/api': 'http://127.0.0.1:8080',
      '/healthz': 'http://127.0.0.1:8080',
      '/ws': {
        target: 'ws://127.0.0.1:8080',
        ws: true,
      },
    },
  },
});
