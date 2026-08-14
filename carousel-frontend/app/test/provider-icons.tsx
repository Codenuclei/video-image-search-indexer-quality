"use client";

/**
 * LLM brand icons via Lobe Icons CDN (@lobehub/icons-static-svg@1.94.0).
 * Mapped from live OpenRouter id prefixes (queried 2026-08-14).
 * Prefer *-color.svg when the pack ships it; else mono.
 */

const LOBE_CDN =
  "https://cdn.jsdelivr.net/npm/@lobehub/icons-static-svg@1.94.0/icons";

/** OpenRouter org prefix (or route provider) → Lobe icon filename */
const PREFIX_TO_FILE: Record<string, string> = {
  openai: "openai.svg",
  anthropic: "anthropic.svg",
  google: "gemini-color.svg",
  gemini: "gemini-color.svg",
  "meta-llama": "meta-color.svg",
  meta: "meta-color.svg",
  mistralai: "mistral-color.svg",
  mistral: "mistral-color.svg",
  cohere: "cohere-color.svg",
  deepseek: "deepseek-color.svg",
  qwen: "qwen-color.svg",
  moonshotai: "kimi-color.svg",
  moonshot: "moonshot.svg",
  kimi: "kimi-color.svg",
  "x-ai": "xai.svg",
  xai: "xai.svg",
  nvidia: "nvidia-color.svg",
  "z-ai": "zai.svg",
  zai: "zai.svg",
  zhipu: "zhipu-color.svg",
  minimax: "minimax-color.svg",
  "bytedance-seed": "bytedance-color.svg",
  bytedance: "bytedance-color.svg",
  openrouter: "openrouter-color.svg",
  perplexity: "perplexity-color.svg",
  amazon: "bedrock-color.svg",
  aws: "aws-color.svg",
  bedrock: "bedrock-color.svg",
  "aion-labs": "aionlabs-color.svg",
  aionlabs: "aionlabs-color.svg",
  poolside: "poolside-color.svg",
  "arcee-ai": "arcee-color.svg",
  arcee: "arcee-color.svg",
  baidu: "baidu-color.svg",
  tencent: "tencent-color.svg",
  microsoft: "microsoft-color.svg",
  azure: "azure-color.svg",
  "ibm-granite": "ibm.svg",
  ibm: "ibm.svg",
  ai21: "ai21.svg",
  liquid: "liquid.svg",
  nousresearch: "nousresearch.svg",
  stepfun: "stepfun-color.svg",
  upstage: "upstage-color.svg",
  kwaipilot: "kwaipilot-color.svg",
  morph: "morph-color.svg",
  relace: "relace.svg",
  allenai: "ai2-color.svg",
  ai2: "ai2-color.svg",
  claude: "anthropic.svg",
  meituan: "longcat-color.svg",
  longcat: "longcat-color.svg",
  inception: "inception.svg",
  deepcogito: "deepcogito.svg",
};

/** Legacy brand union kept for call sites that still pass a brand key. */
export type LlmBrand = string;

function normalizePrefix(raw: string): string {
  let p = raw.trim().toLowerCase();
  if (p.startsWith("~")) p = p.slice(1);
  return p;
}

/** Resolve Lobe icon filename from catalog provider + model id. */
export function resolveLlmIconFile(
  provider: string | undefined,
  modelId: string | undefined
): string | null {
  const id = (modelId || "").trim().toLowerCase();
  const prov = normalizePrefix(provider || "");

  if (id.includes("/")) {
    const org = normalizePrefix(id.split("/")[0] || "");
    if (org && PREFIX_TO_FILE[org]) return PREFIX_TO_FILE[org];
  }

  if (id.startsWith("claude") || prov === "claude") return PREFIX_TO_FILE.anthropic;
  if (id.startsWith("gemini") || id.startsWith("gemma") || prov === "gemini") {
    return PREFIX_TO_FILE.gemini;
  }
  if (prov && PREFIX_TO_FILE[prov]) return PREFIX_TO_FILE[prov];

  if (id.includes("gpt-") || id.includes("o1-") || id.includes("o3-")) {
    return PREFIX_TO_FILE.openai;
  }
  if (id.includes("llama") || id.includes("muse-")) return PREFIX_TO_FILE.meta;
  if (id.includes("mistral") || id.includes("ministral")) return PREFIX_TO_FILE.mistralai;
  if (id.includes("command-") || id.includes("cohere")) return PREFIX_TO_FILE.cohere;
  if (id.includes("qwen")) return PREFIX_TO_FILE.qwen;
  if (id.includes("deepseek")) return PREFIX_TO_FILE.deepseek;

  return null;
}

/** @deprecated prefer resolveLlmIconFile */
export function resolveLlmBrand(
  provider: string | undefined,
  modelId: string | undefined
): LlmBrand {
  const file = resolveLlmIconFile(provider, modelId);
  if (!file) return "unknown";
  return file.replace(/-color\.svg$/, "").replace(/\.svg$/, "");
}

export function lobeIconUrl(file: string): string {
  return `${LOBE_CDN}/${file}`;
}

export function ProviderBrandIcon({
  brand,
  provider,
  modelId,
  className,
  title,
}: {
  brand?: LlmBrand;
  provider?: string;
  modelId?: string;
  className?: string;
  title?: string;
}) {
  let file = resolveLlmIconFile(provider, modelId);
  if (!file && brand && brand !== "unknown") {
    file = PREFIX_TO_FILE[brand] || null;
  }

  if (file) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        className={className}
        src={lobeIconUrl(file)}
        width={14}
        height={14}
        alt=""
        title={title}
        loading="lazy"
        decoding="async"
        referrerPolicy="no-referrer"
      />
    );
  }

  return (
    <svg
      className={className}
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
    >
      {title ? <title>{title}</title> : null}
      <rect
        x="3.5"
        y="3.5"
        width="17"
        height="17"
        rx="4"
        stroke="#D1D5DB"
        strokeWidth="1.5"
      />
      <circle cx="12" cy="12" r="2" fill="#9CA3AF" />
    </svg>
  );
}

export const SIMPLE_ICON_CDN: Record<string, string | null> = {
  openai: lobeIconUrl("openai.svg"),
  anthropic: lobeIconUrl("anthropic.svg"),
  googlegemini: lobeIconUrl("gemini-color.svg"),
  meta: lobeIconUrl("meta-color.svg"),
  mistralai: lobeIconUrl("mistral-color.svg"),
  cohere: lobeIconUrl("cohere-color.svg"),
  deepseek: lobeIconUrl("deepseek-color.svg"),
  qwen: lobeIconUrl("qwen-color.svg"),
  openrouter: lobeIconUrl("openrouter-color.svg"),
  arcee: lobeIconUrl("arcee-color.svg"),
  baidu: lobeIconUrl("baidu-color.svg"),
};
