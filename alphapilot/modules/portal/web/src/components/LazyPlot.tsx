import React, { Suspense } from "react";

const Plot = React.lazy(async () => {
  const [{ default: createPlotlyComponent }, { default: Plotly }] = await Promise.all([
    import("react-plotly.js/factory"),
    import("plotly.js-finance-dist-min"),
  ]);
  return { default: createPlotlyComponent(Plotly) };
});

export function LazyPlot(props: Record<string, unknown>) {
  return (
    <Suspense fallback={<div className="chart-skeleton" aria-busy="true" />}>
      <Plot {...props} />
    </Suspense>
  );
}
