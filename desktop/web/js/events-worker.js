// One event stream for all of a browser's tabs. Each stream holds one of the six connections a
// browser allows to a server, so with one per tab, six tabs would freeze every other request.

const ports = new Set();
let source = null;
let isOpen = false;
let timer = null;

function tell(message) {
  ports.forEach((port) => port.postMessage(message));
}

function connect() {
  source = new EventSource("/api/events");
  source.onopen = () => {
    isOpen = true;
    tell({ type: "open" });
  };
  source.onmessage = (message) => tell({ type: "event", data: message.data });
  source.onerror = () => {
    isOpen = false;
    tell({ type: "lost" });
    // The browser retries by itself unless the stream has closed for good
    if (source.readyState === EventSource.CLOSED) {
      clearTimeout(timer);
      timer = setTimeout(connect, 2000);
    }
  };
}

self.onconnect = (event) => {
  const port = event.ports[0];
  if (typeof EventSource === "undefined") {
    port.postMessage({ type: "unavailable" });
    return;
  }
  ports.add(port);
  if (!source) connect();
  else port.postMessage({ type: isOpen ? "open" : "lost" });
};
