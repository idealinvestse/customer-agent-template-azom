import { copyFileSync, mkdirSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_DIR = resolve(__dirname, '..');
const NODE_MODULES = resolve(FRONTEND_DIR, 'node_modules');
const STATIC_DIR = resolve(FRONTEND_DIR, '..', 'static');
const FONTS_DIR = resolve(STATIC_DIR, 'fonts');
const JS_DIR = resolve(STATIC_DIR, 'js');

mkdirSync(FONTS_DIR, { recursive: true });
mkdirSync(JS_DIR, { recursive: true });

// 1. Copy @alpinejs/csp bundle
const alpineSrc = resolve(NODE_MODULES, '@alpinejs/csp/dist/cdn.min.js');
const alpineDst = resolve(JS_DIR, 'alpine-csp.min.js');
if (!existsSync(alpineSrc)) {
  throw new Error(`Missing @alpinejs/csp bundle at ${alpineSrc}`);
}
copyFileSync(alpineSrc, alpineDst);

// 2. Copy self-hosted WOFF2 fonts (latin + latin-ext for Swedish/Nordic glyphs)
const fontFiles = [
  ['@fontsource/sora/files', 'sora-latin-400-normal.woff2'],
  ['@fontsource/sora/files', 'sora-latin-ext-400-normal.woff2'],
  ['@fontsource/sora/files', 'sora-latin-500-normal.woff2'],
  ['@fontsource/sora/files', 'sora-latin-ext-500-normal.woff2'],
  ['@fontsource/sora/files', 'sora-latin-600-normal.woff2'],
  ['@fontsource/sora/files', 'sora-latin-ext-600-normal.woff2'],
  ['@fontsource/sora/files', 'sora-latin-700-normal.woff2'],
  ['@fontsource/sora/files', 'sora-latin-ext-700-normal.woff2'],
  ['@fontsource/source-sans-3/files', 'source-sans-3-latin-400-normal.woff2'],
  ['@fontsource/source-sans-3/files', 'source-sans-3-latin-ext-400-normal.woff2'],
  ['@fontsource/source-sans-3/files', 'source-sans-3-latin-400-italic.woff2'],
  ['@fontsource/source-sans-3/files', 'source-sans-3-latin-ext-400-italic.woff2'],
  ['@fontsource/source-sans-3/files', 'source-sans-3-latin-500-normal.woff2'],
  ['@fontsource/source-sans-3/files', 'source-sans-3-latin-ext-500-normal.woff2'],
  ['@fontsource/source-sans-3/files', 'source-sans-3-latin-600-normal.woff2'],
  ['@fontsource/source-sans-3/files', 'source-sans-3-latin-ext-600-normal.woff2'],
  ['@fontsource/source-sans-3/files', 'source-sans-3-latin-700-normal.woff2'],
  ['@fontsource/source-sans-3/files', 'source-sans-3-latin-ext-700-normal.woff2'],
  ['@fontsource/jetbrains-mono/files', 'jetbrains-mono-latin-400-normal.woff2'],
  ['@fontsource/jetbrains-mono/files', 'jetbrains-mono-latin-ext-400-normal.woff2'],
  ['@fontsource/jetbrains-mono/files', 'jetbrains-mono-latin-500-normal.woff2'],
  ['@fontsource/jetbrains-mono/files', 'jetbrains-mono-latin-ext-500-normal.woff2'],
  ['@fontsource/jetbrains-mono/files', 'jetbrains-mono-latin-600-normal.woff2'],
  ['@fontsource/jetbrains-mono/files', 'jetbrains-mono-latin-ext-600-normal.woff2'],
];

for (const [pkgDir, filename] of fontFiles) {
  const src = resolve(NODE_MODULES, pkgDir, filename);
  const dst = resolve(FONTS_DIR, filename);
  if (!existsSync(src)) {
    throw new Error(`Missing font asset at ${src}`);
  }
  copyFileSync(src, dst);
}

console.log(`Synced alpine-csp.min.js and ${fontFiles.length} woff2 font files.`);
