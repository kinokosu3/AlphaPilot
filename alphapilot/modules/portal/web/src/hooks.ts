import { useCallback, useEffect, useRef, useState } from "react";
import { buildParams, defaultValuesFor, FieldSpec, FieldValue } from "./paramSpecs";

export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const requestSequence = useRef(0);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    const request = ++requestSequence.current;
    setLoading(true);
    setError(null);
    try {
      const next = await loader();
      if (mounted.current && request === requestSequence.current) setData(next);
    } catch (err) {
      if (mounted.current && request === requestSequence.current) {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      if (mounted.current && request === requestSequence.current) setLoading(false);
    }
  }, deps);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => {
      mounted.current = false;
      requestSequence.current += 1;
    };
  }, [refresh]);

  return { data, error, loading, refresh, setData };
}

export function useSerialPolling<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
  options: { enabled?: boolean; intervalMs?: number } = {},
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const inFlightGeneration = useRef<number | null>(null);
  const mounted = useRef(true);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const requestGeneration = generation.current;
    if (inFlightGeneration.current === requestGeneration) return;
    inFlightGeneration.current = requestGeneration;
    try {
      const next = await loader();
      if (mounted.current && requestGeneration === generation.current) {
        setData(next);
        setError(null);
      }
    } catch (err) {
      if (mounted.current && requestGeneration === generation.current) {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      if (inFlightGeneration.current === requestGeneration) inFlightGeneration.current = null;
      if (mounted.current && requestGeneration === generation.current) setLoading(false);
    }
  }, deps);

  useEffect(() => {
    mounted.current = true;
    generation.current += 1;
    let timer: number | undefined;
    let active = true;
    const interval = Math.max(options.intervalMs ?? 1000, 100);
    const schedule = () => {
      if (!active || !options.enabled || document.hidden) return;
      timer = window.setTimeout(async () => {
        await refresh();
        schedule();
      }, interval);
    };
    const wake = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      if (!document.hidden) void refresh().then(schedule);
    };
    void refresh().then(schedule);
    document.addEventListener("visibilitychange", wake);
    return () => {
      active = false;
      mounted.current = false;
      generation.current += 1;
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [refresh, options.enabled, options.intervalMs]);

  return { data, error, loading, refresh };
}

/**
 * Sequence arbitrary event-driven requests (detail pickers, symbol lists, etc.).
 * The request still settles normally, but callers can only commit a response
 * when ``current`` is true, preventing an older response from overwriting the
 * user's latest selection.
 */
export function useLatestRequest() {
  const sequence = useRef(0);
  const mounted = useRef(true);

  useEffect(() => () => {
    mounted.current = false;
    sequence.current += 1;
  }, []);

  return useCallback(async <T>(loader: () => Promise<T>): Promise<{ current: boolean; data?: T; error?: unknown }> => {
    const request = ++sequence.current;
    try {
      const data = await loader();
      return { current: mounted.current && request === sequence.current, data };
    } catch (error) {
      return { current: mounted.current && request === sequence.current, error };
    }
  }, []);
}

export function useJsonInput(initial = "{}") {
  const [raw, setRaw] = useState(initial);
  const parse = () => {
    if (!raw.trim()) return {};
    const value = JSON.parse(raw);
    if (value === null || Array.isArray(value) || typeof value !== "object") {
      throw new Error("JSON must be an object");
    }
    return value as Record<string, unknown>;
  };
  return { raw, setRaw, parse };
}

export function useParamForm(specs: FieldSpec[], advancedJson = "{}") {
  const [values, setValues] = useState<Record<string, FieldValue>>(() => defaultValuesFor(specs));
  const [errors, setErrors] = useState<Record<string, string>>({});

  const signature = specs.map((field) => [
    field.key,
    String(field.defaultValue ?? ""),
    (field.options || []).map((option) => String(option.value)).join(","),
  ].join(":")).join("|");

  useEffect(() => {
    const defaults = defaultValuesFor(specs);
    setValues((current) => {
      const next: Record<string, FieldValue> = {};
      for (const field of specs) {
        let value = Object.prototype.hasOwnProperty.call(current, field.key) ? current[field.key] : defaults[field.key];
        if (field.type === "select" && field.options?.length) {
          const isAllowed = (candidate: FieldValue) => field.options?.some((option) => String(option.value) === String(candidate));
          if (!isAllowed(value)) {
            value = isAllowed(defaults[field.key]) ? defaults[field.key] : field.options[0].value;
          }
        }
        next[field.key] = value;
      }
      return next;
    });
    setErrors({});
  }, [signature]);

  function setValue(key: string, value: FieldValue) {
    setValues((current) => ({ ...current, [key]: value }));
    setErrors((current) => {
      if (!current[key]) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
  }

  function parse() {
    try {
      setErrors({});
      return buildParams(specs, values, advancedJson);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setErrors({ _form: message });
      throw err;
    }
  }

  return { values, setValue, setValues, errors, parse };
}
