// Build config for the prebuilt static CSS that replaced the Tailwind Play CDN.
// Rebuild after changing/adding utility classes:
//   tailwindcss -i tailwind/input.css -o glider_playground/static/tailwind.css --minify
// (use the standalone Tailwind v4 CLI binary; no Node required)
module.exports = {
  content: [
    "../glider_playground/static/index.html",
    "../glider_playground/static/main_plot.html",
    "../glider_playground/static/map_view.html",
    "../glider_playground/static/3d_view.html",
    "../glider_playground/static/cycle_profile.js",
    "../glider_playground/static/console_log.js",
  ],
  theme: {
    extend: {
      colors: {
        googleBlue: '#41658a', googleBlueHover: '#2e4a68', googleRed: '#ea4335',
        googleYellow: '#fbbc04', googleGreen: '#34a853', bgMain: '#ffffff', panelBg: '#f8f9fa',
      },
      borderRadius: { DEFAULT: '4px' },
    },
  },
};
