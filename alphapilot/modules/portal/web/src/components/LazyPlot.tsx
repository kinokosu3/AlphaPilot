import React, { Suspense } from "react";

const Plot = React.lazy(() => import("react-plotly.js"));

export function LazyPlot(props: Record<string, unknown>) {
  return (
    <Suspense fallback={<div className="chart-skeleton" aria-busy="true" />}>
      <Plot {...props} />
    </Suspense>
  );
}
