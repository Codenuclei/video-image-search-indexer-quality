import { describe, expect, it } from "vitest";
import { resolveArenaCreativeWritingCatalog } from "./arena-creative-writing-top10";

describe("resolveArenaCreativeWritingCatalog", () => {
  it("keeps Claude-direct only when the live Anthropic catalog has that id", () => {
    const out = resolveArenaCreativeWritingCatalog([
      { id: "claude-sonnet-4-5-20250929", label: "Sonnet", provider: "claude" },
      { id: "anthropic/claude-fable-5", label: "Fable", provider: "openrouter" },
    ]);
    const fable = out.find((row) => row.label.startsWith("#1"));
    expect(fable).toEqual({
      id: "anthropic/claude-fable-5",
      label: "#1 Claude Fable 5 · OpenRouter",
      provider: "openrouter",
    });
  });

  it("does not treat an OpenRouter id as proof a direct Claude model exists", () => {
    const out = resolveArenaCreativeWritingCatalog([
      { id: "claude-fable-5", label: "Fable via OR", provider: "openrouter" },
    ]);
    const fable = out.find((row) => row.label.startsWith("#1"));
    expect(fable?.provider).toBe("openrouter");
    expect(fable?.id).toBe("anthropic/claude-fable-5");
  });

  it("keeps a Gemini-direct row when the live Gemini catalog lists it", () => {
    const out = resolveArenaCreativeWritingCatalog([
      { id: "gemini-3.7-flash", label: "Flash", provider: "gemini" },
    ]);
    const flash = out.find((row) => row.label.includes("Gemini 3.7 Flash"));
    expect(flash).toEqual({
      id: "gemini-3.7-flash",
      label: "#3 Gemini 3.7 Flash (high)",
      provider: "gemini",
    });
  });
});
