// Live mode only: applies style changes in place and reloads the page for any other change.

const CHECK_MS = 200;
let seen = null;

function swapStyles(version) {
  document.querySelectorAll('link[rel="stylesheet"]').forEach((old) => {
    const fresh = old.cloneNode();
    fresh.href = `${old.href.split("?")[0]}?v=${version}`;
    fresh.onload = () => old.remove();
    old.after(fresh);
  });
}

async function check() {
  try {
    const response = await fetch("/__live/version");
    const now = await response.json();
    if (seen && now.page !== seen.page) {
      location.reload();
      return;
    }
    if (seen && now.styles !== seen.styles) swapStyles(now.styles);
    seen = now;
  } catch {
    // The server is restarting
  }
  setTimeout(check, CHECK_MS);
}

check();
