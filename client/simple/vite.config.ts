// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * CONFIG: https://vite.dev/config/
 */

import { resolve } from "node:path";
import { constants as zlibConstants } from "node:zlib";
import browserslistToEsbuild from "browserslist-to-esbuild";
import { browserslistToTargets } from "lightningcss";
import type { PreRenderedAsset } from "rolldown";
import type { Config } from "svgo";
import type { UserConfig } from "vite";
import analyzer from "vite-bundle-analyzer";
import manifest from "./package.json" with { type: "json" };
import { plg_svg2png, plg_svg2svg } from "./tools/plg.ts";

const ROOT = "../../"; // root of the git repository

const PATH = {
  brand: "src/brand/",
  dist: resolve(ROOT, "searx/static/themes/simple/"),
  modules: "node_modules/",
  src: "src/",
  templates: resolve(ROOT, "searx/templates/simple/")
} as const;

const svg2svg_opts: Config = {
  plugins: [{ name: "preset-default" }, "sortAttrs", "convertStyleToAttrs"]
};

const svg2svg_favicon_opts: Config = {
  plugins: [{ name: "preset-default" }, "sortAttrs"]
};

export default {
  base: "./",
  publicDir: "static/",

  build: {
    target: browserslistToEsbuild(manifest.browserslist),
    assetsDir: "",
    outDir: PATH.dist,
    manifest: "manifest.json",
    emptyOutDir: true,
    sourcemap: true,
    rolldownOptions: {
      input: {
        // entrypoint
        core: `${PATH.src}/js/index.ts`,

        // stylesheets
        ltr: `${PATH.src}/less/style-ltr.less`,
        rtl: `${PATH.src}/less/style-rtl.less`,
        rss: `${PATH.src}/less/rss.less`
      },

      // file naming conventions / pathnames are relative to outDir (PATH.dist)
      output: {
        // Coalesce the many tiny per-module chunks the router dynamic-imports
        // (~9 files = ~9 edge-Worker requests on a cold results page) into two,
        // WITHOUT changing evaluation semantics. A chunk evaluates every module
        // it contains as soon as anything imports it, and the main/* controllers
        // run unguarded top-level DOM assertions (e.g. search.ts does
        // getElement("search"), which throws when absent) — so what may share a
        // chunk is constrained by which endpoints load it:
        //
        //   • "app" — the controllers loaded ONLY on the index/results endpoints
        //     (both of which have #search) plus the InfiniteScroll plugin (a
        //     side-effect-free class). Safe to co-evaluate; never imported by the
        //     preferences page.
        //   • "util" — the side-effect-free helpers. preferences.ts imports these,
        //     so they MUST stay out of "app" (else loading preferences would
        //     evaluate search.ts and throw).
        //
        // includeDependenciesRecursively:false keeps each group to its matched
        // modules so the shared toolkit isn't dragged in — that would make core
        // statically import "app" and evaluate the controllers on every page.
        // The heavy on-demand plugins are intentionally unmatched and stay lazy:
        // MapView (~324KB, OpenLayers) and Calculator (~146KB, mathjs).
        codeSplitting: {
          includeDependenciesRecursively: false,
          groups: [
            {
              name: "app",
              test: /src[\\/]js[\\/](main[\\/](keyboard|results|search|autocomplete|favicons)|plugin[\\/]InfiniteScroll)\.ts$/,
              minSize: 0,
              priority: 100
            },
            {
              name: "util",
              test: /src[\\/]js[\\/]util[\\/]/,
              minSize: 0,
              priority: 50
            }
          ]
        },
        entryFileNames: "sxng-[name].min.js",
        chunkFileNames: "chunk/[hash].min.js",
        assetFileNames: ({ names }: PreRenderedAsset): string => {
          const [name] = names;

          switch (name?.split(".").pop()) {
            case "css":
              return "sxng-[name].min[extname]";
            default:
              return "sxng-[name][extname]";
          }
        },
        sanitizeFileName: (name: string): string => {
          return name
            .normalize("NFD")
            .replace(/[^a-zA-Z0-9.-]/g, "_")
            .toLowerCase();
        },
        comments: {
          legal: true
        }
      }
    }
  }, // end: build

  plugins: [
    // -- bundle analyzer
    analyzer({
      enabled: process.env.VITE_BUNDLE_ANALYZE === "true",
      analyzerPort: "auto",
      summary: true,
      reportTitle: manifest.name,

      // sidecars with max compression
      gzipOptions: {
        level: zlibConstants.Z_BEST_COMPRESSION
      },
      brotliOptions: {
        params: {
          [zlibConstants.BROTLI_PARAM_QUALITY]: zlibConstants.BROTLI_MAX_QUALITY
        }
      }
    }),

    // -- svg images
    plg_svg2svg(
      [
        {
          src: `${PATH.src}/svg/empty_favicon.svg`,
          dest: `${PATH.dist}/img/empty_favicon.svg`
        },
        {
          src: `${PATH.src}/svg/select-dark.svg`,
          dest: `${PATH.dist}/img/select-dark.svg`
        },
        {
          src: `${PATH.src}/svg/select-light.svg`,
          dest: `${PATH.dist}/img/select-light.svg`
        }
      ],
      svg2svg_opts
    ),

    // SearXNG brand (static)
    plg_svg2png([
      {
        src: `${PATH.brand}/searxng-wordmark.svg`,
        dest: `${PATH.dist}/img/favicon.png`
      },
      {
        src: `${PATH.brand}/searxng.svg`,
        dest: `${PATH.dist}/img/searxng.png`
      }
    ]),

    // SearXNG PWA Icons (static)
    plg_svg2png(
      [
        {
          src: `${PATH.brand}/searxng-wordmark.svg`,
          dest: `${PATH.dist}/img/512.png`
        }
      ],
      512,
      512
    ),
    plg_svg2png(
      [
        {
          src: `${PATH.brand}/searxng-wordmark.svg`,
          dest: `${PATH.dist}/img/192.png`
        }
      ],
      192,
      192
    ),

    // -- svg
    plg_svg2svg(
      [
        {
          src: `${PATH.brand}/searxng.svg`,
          dest: `${PATH.dist}/img/searxng.svg`
        },
        {
          src: `${PATH.brand}/img_load_error.svg`,
          dest: `${PATH.dist}/img/img_load_error.svg`
        }
      ],
      svg2svg_opts
    ),

    // -- favicon
    plg_svg2svg(
      [
        {
          src: `${PATH.brand}/searxng-wordmark.svg`,
          dest: `${PATH.dist}/img/favicon.svg`
        }
      ],
      svg2svg_favicon_opts
    ),

    // -- simple templates
    plg_svg2svg(
      [
        {
          src: `${PATH.brand}/searxng-wordmark.svg`,
          dest: `${PATH.templates}/searxng-wordmark.min.svg`
        }
      ],
      svg2svg_opts
    )
  ], // end: plugins

  // FIXME: missing CCS sourcemaps!!
  // see: https://github.com/vitejs/vite/discussions/13845#discussioncomment-11992084
  //
  // what I have tried so far (see config below):
  //
  // - build.sourcemap
  // - esbuild.sourcemap
  // - css.preprocessorOptions.less.sourceMap
  css: {
    transformer: "lightningcss",
    lightningcss: {
      targets: browserslistToTargets(manifest.browserslist)
    },
    devSourcemap: true
  } // end: css
} satisfies UserConfig;
