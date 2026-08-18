import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

// Theme is applied before first paint so a reload does not flash the wrong one.
const stored = localStorage.getItem("studylink.theme");
document.documentElement.setAttribute("data-theme", stored ?? "dark");

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
