// Builds static/css/tailwind.css (served to the browser) from the classes used in
// the templates and app.js. Rebuild after adding or changing classes:
//   npm install && npm run build:css
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./templates/**/*.html", "./static/js/**/*.js"],
  theme: {
    extend: {
      colors: {
        darkbg: "#0b0f19",
        darkcard: "#111827",
        darkborder: "#1f293d",
        gold: {
          400: "#fbbf24",
          500: "#f59e0b",
          600: "#d97706",
        },
      },
    },
  },
};
