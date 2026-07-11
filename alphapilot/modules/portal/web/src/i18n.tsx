import React, { createContext, useContext, useMemo, useState } from "react";
import { en } from "./i18n/en";
import type { Lang } from "./i18n/types";
import { zh } from "./i18n/zh";

const dictionaries = { zh, en };

const I18nContext = createContext<{ lang: Lang; setLang: (lang: Lang) => void; t: (key: string) => string } | null>(null);

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLang] = useState<Lang>(() => (localStorage.getItem("portal_lang") as Lang) || "zh");
  const value = useMemo(
    () => ({
      lang,
      setLang(next: Lang) {
        localStorage.setItem("portal_lang", next);
        setLang(next);
      },
      t(key: string) {
        return dictionaries[lang][key] || dictionaries.zh[key] || key;
      }
    }),
    [lang]
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error("I18nProvider missing");
  return ctx;
}
