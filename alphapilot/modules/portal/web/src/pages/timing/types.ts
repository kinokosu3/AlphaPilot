import type { Job } from "../../api";

export type TablePreview = {
  columns: string[];
  rows: Array<Record<string, unknown>>;
  row_count?: number;
  truncated?: boolean;
  missing?: boolean;
};

export type TimingStrategySpec = {
  name: string;
  description: string;
  defaults: Record<string, unknown>;
  parameter_schema?: {
    properties?: Record<string, {
      type?: "integer" | "number" | "boolean" | "string";
      default?: unknown;
      minimum?: number;
      maximum?: number;
      description?: string;
    }>;
    required?: string[];
  };
  required_history?: number;
  version?: string;
  source?: string;
  code_hash?: string;
};

export type TimingStrategiesPayload = {
  strategies: TimingStrategySpec[];
  names: string[];
};

export type TimingSignalPayload = {
  strategy_name: string;
  signals: TablePreview;
};

export type TimingDetailPayload = {
  job: Job;
  summary: Record<string, unknown>;
  artifact_dir: string;
  signals: TablePreview;
  trades: TablePreview;
  equity_curve: TablePreview;
  positions: TablePreview;
};
