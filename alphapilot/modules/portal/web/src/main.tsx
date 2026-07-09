import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { ConfirmProvider, Layout } from "./components";
import { I18nProvider } from "./i18n";
import { HomePage } from "./pages/home/HomePage";
import { ToastProvider } from "./toast";
import "./styles.css";

const AdvancedPage = React.lazy(() => import("./pages").then((module) => ({ default: module.AdvancedPage })));
const BacktestPage = React.lazy(() => import("./pages").then((module) => ({ default: module.BacktestPage })));
const DailyTradePage = React.lazy(() => import("./pages").then((module) => ({ default: module.DailyTradePage })));
const LibraryPage = React.lazy(() => import("./pages").then((module) => ({ default: module.LibraryPage })));
const LivePage = React.lazy(() => import("./pages/live/LivePage").then((module) => ({ default: module.LivePage })));
const MarketPage = React.lazy(() => import("./pages").then((module) => ({ default: module.MarketPage })));
const MiningPage = React.lazy(() => import("./pages").then((module) => ({ default: module.MiningPage })));
const NotificationsPage = React.lazy(() => import("./pages").then((module) => ({ default: module.NotificationsPage })));
const SchedulerPage = React.lazy(() => import("./pages").then((module) => ({ default: module.SchedulerPage })));
const TimingPage = React.lazy(() => import("./pages").then((module) => ({ default: module.TimingPage })));

function lazyElement(Page: React.ComponentType) {
  return (
    <React.Suspense fallback={<div className="route-skeleton" aria-busy="true" />}>
      <Page />
    </React.Suspense>
  );
}

// Apply the saved theme before first paint to avoid a flash of the wrong theme.
document.documentElement.dataset.theme = localStorage.getItem("portal_theme") === "dark" ? "dark" : "light";

const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "mining", element: lazyElement(MiningPage) },
      { path: "backtest", element: lazyElement(BacktestPage) },
      { path: "timing", element: lazyElement(TimingPage) },
      { path: "library", element: lazyElement(LibraryPage) },
      { path: "market", element: lazyElement(MarketPage) },
      { path: "daily-trade", element: lazyElement(DailyTradePage) },
      { path: "live", element: lazyElement(LivePage) },
      { path: "scheduler", element: lazyElement(SchedulerPage) },
      { path: "notifications", element: lazyElement(NotificationsPage) },
      { path: "advanced", element: lazyElement(AdvancedPage) }
    ]
  }
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <I18nProvider>
      <ToastProvider>
        <ConfirmProvider>
          <RouterProvider router={router} />
        </ConfirmProvider>
      </ToastProvider>
    </I18nProvider>
  </React.StrictMode>
);
