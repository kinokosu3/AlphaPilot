declare module "react-plotly.js" {
  import * as React from "react";

  export default class Plot extends React.Component<Record<string, unknown>> {}
}

declare module "react-plotly.js/factory" {
  import * as React from "react";

  export default function createPlotlyComponent(plotly: unknown): React.ComponentType<Record<string, unknown>>;
}

declare module "plotly.js-finance-dist-min" {
  const Plotly: unknown;
  export default Plotly;
}
