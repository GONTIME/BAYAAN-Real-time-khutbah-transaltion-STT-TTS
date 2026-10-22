import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import RoomPage from "./App";
import Dashboard from "./Dashboard";
import ProjectorView from "./ProjectorView";
import LoginPage from "./LoginPage";
import { isAuthenticated } from "./auth";

// PWA service worker (§191): offline shell; /api and /ws bypass the cache.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* SW is a progressive enhancement - the app works without it */
    });
  });
}

// Router:
// /login -> Operator sign-in page
// /r/<CODE> -> Worshipper mobile PWA (§191)
// /projector/<CODE> -> Mosque wall projector screen
// Everything else (/ , /dashboard, /admin, /operator) -> Unified Mosque Operator Dashboard
const path = location.pathname;
let Page = Dashboard;
if (path.startsWith("/login")) {
  Page = LoginPage;
} else if (path.startsWith("/r/")) {
  Page = RoomPage;
} else if (path.startsWith("/projector")) {
  Page = ProjectorView;
} else {
  // Guard the Mosque Operations Cockpit
  if (!isAuthenticated()) {
    Page = LoginPage;
  } else {
    Page = Dashboard;
  }
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Page />
  </StrictMode>
);
