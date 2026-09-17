// Source for static/vendor/tailwind.css. Rebuild after adding/changing utility classes:
//   tailwindcss -i tailwind/input.css -o glider_playground/static/vendor/tailwind.css --minify
module.exports = {
  content: [
    "../glider_playground/static/*.html",
    "../glider_playground/static/js/*.js",
    "../glider_playground/static/map_view/*.js",
  ],
  theme: {
    extend: {
      // App colours: `text-blue`, `text-red`, `text-green` (the shaded `blue-500` etc. still work).
      colors: {
        blue: { DEFAULT: '#41658a' },
        red: { DEFAULT: '#ea4335' },
        green: { DEFAULT: '#34a853' },
      },
      borderRadius: { DEFAULT: '4px' },
    },
  },
};
