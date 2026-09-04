import {
  apiAssetUrl,
  cacheOnlyAssetUrl,
  type CarouselOutlineSlide,
  type CarouselSlidePanel,
} from "./api";

export const CAROUSEL_EXPORT_WIDTH = 1080;
export const CAROUSEL_EXPORT_HEIGHT = 1350;
export const CAROUSEL_JPEG_QUALITY = 0.92;

export type CarouselExportLayout = "single_1" | "split_2";
export type CarouselExportPreset = "default" | "mu_event_photo";

export type CarouselRenderOptions = {
  preset?: CarouselExportPreset;
  title?: string | null;
  speakerName?: string | null;
  /** A distinct neighboring selection used as the second body photo. */
  secondarySlide?: CarouselOutlineSlide | null;
};

export type CoverCrop = {
  sx: number;
  sy: number;
  sw: number;
  sh: number;
};

type TextStyle = {
  fontSize: number;
  minFontSize: number;
  fontWeight: number;
  lineHeight: number;
  maxLines: number;
  maxWidth: number;
};

type CaptionWord = {
  text: string;
  highlighted: boolean;
};

type LoadedImage = {
  image: HTMLImageElement;
  release: () => void;
};

export class CarouselExportError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CarouselExportError";
  }
}

export function clampUnit(value: number | null | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.min(1, Math.max(0, value))
    : fallback;
}

/** Source rectangle for a focal-point-aware object-fit: cover crop. */
export function computeCoverCrop(
  sourceWidth: number,
  sourceHeight: number,
  targetWidth: number,
  targetHeight: number,
  focalX?: number | null,
  focalY?: number | null
): CoverCrop {
  const scale = Math.max(targetWidth / sourceWidth, targetHeight / sourceHeight);
  const sw = targetWidth / scale;
  const sh = targetHeight / scale;
  const desiredX = clampUnit(focalX, 0.5) * sourceWidth - sw / 2;
  const desiredY = clampUnit(focalY, 0.4) * sourceHeight - sh / 2;
  return {
    sx: Math.min(Math.max(desiredX, 0), Math.max(0, sourceWidth - sw)),
    sy: Math.min(Math.max(desiredY, 0), Math.max(0, sourceHeight - sh)),
    sw,
    sh,
  };
}

export function sanitizeExportFilename(value: string): string {
  return (
    value
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 64) || "instagram-carousel"
  );
}

export function carouselSlideFilename(title: string, slideIndex: number): string {
  return `${sanitizeExportFilename(title)}-${String(slideIndex + 1).padStart(2, "0")}.jpg`;
}

export function carouselSlideFilenames(title: string, slideCount: number): string[] {
  return Array.from(
    { length: Math.max(0, Math.floor(slideCount)) },
    (_, index) => carouselSlideFilename(title, index)
  );
}

export function carouselArchiveFilename(title: string): string {
  return `${sanitizeExportFilename(title)}-carousel.zip`;
}

export function resolveHighlightIndices(
  text: string,
  highlight?: number[] | null,
  highlightWords?: string[] | null
): Set<number> {
  const words = text.trim().split(/\s+/).filter(Boolean);
  const result = new Set<number>();
  for (const raw of highlight ?? []) {
    const index = Number(raw);
    if (Number.isInteger(index) && index >= 0 && index < words.length) result.add(index);
  }
  if (result.size || !highlightWords?.length) return result;

  const normalize = (word: string) =>
    word.toLowerCase().replace(/^[^a-z0-9']+|[^a-z0-9']+$/gi, "");
  const normalizedWords = words.map(normalize);
  for (const requested of highlightWords) {
    const normalized = normalize(String(requested || ""));
    if (!normalized) continue;
    const index = normalizedWords.findIndex((word, i) => word === normalized && !result.has(i));
    if (index >= 0) result.add(index);
  }
  return result;
}

function imageIdentity(rawUrl: string): string {
  const value = rawUrl.trim();
  try {
    const parsed = new URL(value, "http://carousel.local");
    parsed.searchParams.delete("cache_only");
    parsed.searchParams.sort();
    return `${parsed.origin}${parsed.pathname}?${parsed.searchParams.toString()}`.replace(/\?$/, "");
  } catch {
    return value;
  }
}

function usableSplitPanels(slide: CarouselOutlineSlide): CarouselSlidePanel[] | null {
  const panels = slide.panels?.slice(0, 2);
  const firstUrl = panels?.[0]?.preview_url?.trim();
  const secondUrl = panels?.[1]?.preview_url?.trim();
  if (
    !panels ||
    panels.length < 2 ||
    !firstUrl ||
    !secondUrl ||
    imageIdentity(firstUrl) === imageIdentity(secondUrl)
  ) {
    return null;
  }
  return panels;
}

export function validateSlideForExport(
  slide: CarouselOutlineSlide,
  layout: CarouselExportLayout,
  slideNumber: number,
  preset: CarouselExportPreset = "default"
): void {
  // The MU preset deliberately supports an intentional black text-only fallback.
  if (preset === "mu_event_photo") return;
  if (layout === "split_2") {
    if (!usableSplitPanels(slide)) {
      throw new CarouselExportError(
        `Slide ${slideNumber} needs two distinct selected panel images for Split panels. ` +
          "Select two frames or switch the layout to Single image."
      );
    }
    return;
  }
  if (!slide.preview_url?.trim()) {
    throw new CarouselExportError(
      `Slide ${slideNumber} has no selected image. Select a frame, upload an image, or choose a reference image before exporting.`
    );
  }
}

function slideImageUrl(slide: CarouselOutlineSlide, rawUrl: string): string {
  const url = rawUrl.trim();
  return slide.frame_source === "manual" ? apiAssetUrl(url) : cacheOnlyAssetUrl(url);
}

async function loadExportImage(url: string, slideNumber: number): Promise<LoadedImage> {
  let response: Response;
  try {
    response = await fetch(url, { mode: "cors" });
  } catch {
    throw new CarouselExportError(
      `Slide ${slideNumber} image could not be fetched. Check the network and ensure the image host allows CORS: ${url}`
    );
  }
  if (!response.ok) {
    throw new CarouselExportError(
      `Slide ${slideNumber} image request failed (${response.status}). Re-select the frame or use a fetchable image URL: ${url}`
    );
  }

  let blob: Blob;
  try {
    blob = await response.blob();
  } catch {
    throw new CarouselExportError(
      `Slide ${slideNumber} image download was interrupted. Check the network and try again: ${url}`
    );
  }
  if (!blob.size) {
    throw new CarouselExportError(`Slide ${slideNumber} image response was empty: ${url}`);
  }
  const objectUrl = URL.createObjectURL(blob);
  const image = new Image();
  try {
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error("decode failed"));
      image.src = objectUrl;
    });
  } catch {
    URL.revokeObjectURL(objectUrl);
    throw new CarouselExportError(
      `Slide ${slideNumber} image could not be decoded. Use a valid JPEG, PNG, WebP, GIF, or fetchable reference image: ${url}`
    );
  }
  if (!image.naturalWidth || !image.naturalHeight) {
    URL.revokeObjectURL(objectUrl);
    throw new CarouselExportError(`Slide ${slideNumber} image has invalid dimensions: ${url}`);
  }
  return { image, release: () => URL.revokeObjectURL(objectUrl) };
}

function drawCoverImage(
  context: CanvasRenderingContext2D,
  image: HTMLImageElement,
  x: number,
  y: number,
  width: number,
  height: number,
  focalX?: number | null,
  focalY?: number | null
) {
  const crop = computeCoverCrop(
    image.naturalWidth,
    image.naturalHeight,
    width,
    height,
    focalX,
    focalY
  );
  context.drawImage(image, crop.sx, crop.sy, crop.sw, crop.sh, x, y, width, height);
}

function captionWords(
  text: string,
  highlight?: number[] | null,
  highlightWords?: string[] | null
): CaptionWord[] {
  const cleanWords = text.trim().split(/\s+/).filter(Boolean);
  const highlighted = resolveHighlightIndices(text, highlight, highlightWords);
  return cleanWords.map((word, index) => ({ text: word, highlighted: highlighted.has(index) }));
}

function setFont(context: CanvasRenderingContext2D, size: number, weight: number) {
  context.font = `${weight} ${size}px Arial, Helvetica, sans-serif`;
}

function wrapCaption(
  context: CanvasRenderingContext2D,
  words: CaptionWord[],
  maxWidth: number
): CaptionWord[][] {
  const lines: CaptionWord[][] = [];
  let line: CaptionWord[] = [];
  for (const word of words) {
    const candidate = [...line, word];
    const width = context.measureText(candidate.map((item) => item.text).join(" ")).width;
    if (line.length && width > maxWidth) {
      lines.push(line);
      line = [word];
    } else {
      line = candidate;
    }
  }
  if (line.length) lines.push(line);
  return lines;
}

function fitCaption(
  context: CanvasRenderingContext2D,
  words: CaptionWord[],
  style: TextStyle
): { lines: CaptionWord[][]; fontSize: number } {
  let fontSize = style.fontSize;
  let lines: CaptionWord[][] = [];
  while (fontSize >= style.minFontSize) {
    setFont(context, fontSize, style.fontWeight);
    lines = wrapCaption(context, words, style.maxWidth);
    if (lines.length <= style.maxLines) break;
    fontSize -= 2;
  }
  if (lines.length > style.maxLines) {
    lines = lines.slice(0, style.maxLines);
    const last = lines[lines.length - 1];
    if (last?.length) last[last.length - 1] = { ...last[last.length - 1], text: `${last[last.length - 1].text}…` };
  }
  return { lines, fontSize: Math.max(fontSize, style.minFontSize) };
}

function drawCaption(
  context: CanvasRenderingContext2D,
  text: string,
  x: number,
  bottom: number,
  style: TextStyle,
  highlight?: number[] | null,
  highlightWords?: string[] | null
) {
  const words = captionWords(text, highlight, highlightWords);
  if (!words.length) return;
  const fitted = fitCaption(context, words, style);
  const lineHeight = fitted.fontSize * style.lineHeight;
  const firstBaseline = bottom - (fitted.lines.length - 1) * lineHeight;

  context.textBaseline = "alphabetic";
  context.shadowColor = "rgba(0, 0, 0, 0.72)";
  context.shadowBlur = 12;
  context.shadowOffsetY = 3;
  setFont(context, fitted.fontSize, style.fontWeight);
  fitted.lines.forEach((line, lineIndex) => {
    let cursorX = x;
    const baseline = firstBaseline + lineIndex * lineHeight;
    line.forEach((word, wordIndex) => {
      if (wordIndex) cursorX += context.measureText(" ").width;
      context.fillStyle = word.highlighted ? "#ffe600" : "#ffffff";
      context.fillText(word.text, cursorX, baseline);
      cursorX += context.measureText(word.text).width;
    });
  });
  context.shadowColor = "transparent";
  context.shadowBlur = 0;
  context.shadowOffsetY = 0;
}

function drawSingleScrim(context: CanvasRenderingContext2D) {
  const gradient = context.createLinearGradient(0, 500, 0, CAROUSEL_EXPORT_HEIGHT);
  gradient.addColorStop(0, "rgba(0,0,0,0)");
  gradient.addColorStop(0.34, "rgba(0,0,0,0.18)");
  gradient.addColorStop(0.65, "rgba(0,0,0,0.72)");
  gradient.addColorStop(1, "rgba(0,0,0,0.96)");
  context.fillStyle = gradient;
  context.fillRect(0, 0, CAROUSEL_EXPORT_WIDTH, CAROUSEL_EXPORT_HEIGHT);
}

function drawPanelScrim(context: CanvasRenderingContext2D, y: number, height: number) {
  const gradient = context.createLinearGradient(0, y + height * 0.2, 0, y + height);
  gradient.addColorStop(0, "rgba(0,0,0,0)");
  gradient.addColorStop(0.35, "rgba(0,0,0,0.16)");
  gradient.addColorStop(0.7, "rgba(0,0,0,0.66)");
  gradient.addColorStop(1, "rgba(0,0,0,0.94)");
  context.fillStyle = gradient;
  context.fillRect(0, y, CAROUSEL_EXPORT_WIDTH, height);
}

function roundedRect(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number
) {
  context.beginPath();
  context.moveTo(x + radius, y);
  context.lineTo(x + width - radius, y);
  context.quadraticCurveTo(x + width, y, x + width, y + radius);
  context.lineTo(x + width, y + height - radius);
  context.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
  context.lineTo(x + radius, y + height);
  context.quadraticCurveTo(x, y + height, x, y + height - radius);
  context.lineTo(x, y + radius);
  context.quadraticCurveTo(x, y, x + radius, y);
  context.closePath();
}

function drawSlideNumber(
  context: CanvasRenderingContext2D,
  slideIndex: number,
  slideCount: number
) {
  const label = String(slideIndex + 1).padStart(2, "0");
  const x = 892;
  const y = 52;
  const width = 126;
  const height = 58;
  roundedRect(context, x, y, width, height, height / 2);
  context.fillStyle = "rgba(0,0,0,0.56)";
  context.fill();
  context.strokeStyle = "rgba(255,255,255,0.34)";
  context.lineWidth = 2;
  context.stroke();
  context.fillStyle = "#ffffff";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.shadowColor = "rgba(0,0,0,0.5)";
  context.shadowBlur = 6;
  setFont(context, 27, 750);
  context.fillText(label, x + width / 2, y + height / 2 + 1);
  context.textAlign = "left";
  context.textBaseline = "alphabetic";
  context.shadowColor = "transparent";
  context.shadowBlur = 0;

  // Keep the total encoded in the deterministic drawing path without adding preview-only copy.
  void slideCount;
}

function splitPanelCaptions(slide: CarouselOutlineSlide, panels: CarouselSlidePanel[]): string[] {
  const fallback = (slide.transcript_text || slide.hook_line || "").trim();
  const captions = panels.map((panel, index) =>
    (panel.caption || (index === 0 ? fallback : "") || "").trim()
  );
  if (captions[0] && captions[0] === captions[1]) captions[1] = "";
  return captions;
}

async function renderSingle(
  context: CanvasRenderingContext2D,
  slide: CarouselOutlineSlide,
  slideNumber: number,
  role: "cover" | "body" | "final"
) {
  const url = slideImageUrl(slide, slide.preview_url!);
  const loaded = await loadExportImage(url, slideNumber);
  try {
    drawCoverImage(
      context,
      loaded.image,
      0,
      0,
      CAROUSEL_EXPORT_WIDTH,
      CAROUSEL_EXPORT_HEIGHT,
      slide.focal_x,
      slide.focal_y
    );
  } finally {
    loaded.release();
  }
  drawSingleScrim(context);

  const styles: Record<typeof role, TextStyle> = {
    cover: {
      fontSize: 86,
      minFontSize: 58,
      fontWeight: 750,
      lineHeight: 1.08,
      maxLines: 4,
      maxWidth: 760,
    },
    body: {
      fontSize: 58,
      minFontSize: 42,
      fontWeight: 650,
      lineHeight: 1.3,
      maxLines: 3,
      maxWidth: 850,
    },
    final: {
      fontSize: 72,
      minFontSize: 50,
      fontWeight: 720,
      lineHeight: 1.15,
      maxLines: 3,
      maxWidth: 820,
    },
  };
  drawCaption(
    context,
    slide.transcript_text || slide.hook_line || "",
    86,
    1208,
    styles[role],
    slide.highlight,
    slide.highlight_words
  );
}

async function renderSplit(
  context: CanvasRenderingContext2D,
  slide: CarouselOutlineSlide,
  slideNumber: number
) {
  const panels = usableSplitPanels(slide)!;
  const captions = splitPanelCaptions(slide, panels);
  const panelHeight = CAROUSEL_EXPORT_HEIGHT / 2;
  for (let index = 0; index < 2; index++) {
    const panel = panels[index];
    const y = index * panelHeight;
    const loaded = await loadExportImage(slideImageUrl(slide, panel.preview_url!), slideNumber);
    try {
      drawCoverImage(
        context,
        loaded.image,
        0,
        y,
        CAROUSEL_EXPORT_WIDTH,
        panelHeight,
        panel.focal_x,
        panel.focal_y
      );
    } finally {
      loaded.release();
    }
    drawPanelScrim(context, y, panelHeight);
    drawCaption(
      context,
      captions[index] || "",
      64,
      y + panelHeight - 62,
      {
        fontSize: 45,
        minFontSize: 34,
        fontWeight: 650,
        lineHeight: 1.28,
        maxLines: 3,
        maxWidth: 950,
      },
      panel.highlight ?? slide.highlight,
      panel.highlight_words ?? slide.highlight_words
    );
  }
  context.fillStyle = "rgba(255,255,255,0.18)";
  context.fillRect(0, panelHeight - 1, CAROUSEL_EXPORT_WIDTH, 2);
}

function drawMuFade(
  context: CanvasRenderingContext2D,
  y: number,
  height: number,
  top = true,
  bottom = true
) {
  if (top) {
    const fade = context.createLinearGradient(0, y, 0, y + height * 0.28);
    fade.addColorStop(0, "rgba(0,0,0,0.86)");
    fade.addColorStop(1, "rgba(0,0,0,0)");
    context.fillStyle = fade;
    context.fillRect(0, y, CAROUSEL_EXPORT_WIDTH, height * 0.3);
  }
  if (bottom) {
    const fade = context.createLinearGradient(
      0,
      y + height * 0.52,
      0,
      y + height
    );
    fade.addColorStop(0, "rgba(0,0,0,0)");
    fade.addColorStop(1, "rgba(0,0,0,0.96)");
    context.fillStyle = fade;
    context.fillRect(0, y + height * 0.48, CAROUSEL_EXPORT_WIDTH, height * 0.52);
  }
}

function drawCenteredCaption(
  context: CanvasRenderingContext2D,
  slide: CarouselOutlineSlide,
  text: string,
  centerY: number,
  maxLines: number,
  fontSize: number
) {
  const words = captionWords(text, slide.highlight, slide.highlight_words);
  if (!words.length) return;
  const fitted = fitCaption(context, words, {
    fontSize,
    minFontSize: 42,
    fontWeight: 720,
    lineHeight: 1.12,
    maxLines,
    maxWidth: 900,
  });
  const lineHeight = fitted.fontSize * 1.12;
  const blockHeight = fitted.lines.length * lineHeight;
  let baseline = centerY - blockHeight / 2 + fitted.fontSize;
  context.textAlign = "left";
  context.textBaseline = "alphabetic";
  context.shadowColor = "rgba(0,0,0,0.82)";
  context.shadowBlur = 14;
  setFont(context, fitted.fontSize, 720);
  for (const line of fitted.lines) {
    const widths = line.map((word) => context.measureText(word.text).width);
    const space = context.measureText(" ").width;
    const width = widths.reduce((sum, value) => sum + value, 0) +
      Math.max(0, line.length - 1) * space;
    let x = (CAROUSEL_EXPORT_WIDTH - width) / 2;
    line.forEach((word, index) => {
      if (index) x += space;
      context.fillStyle = word.highlighted ? "#ffe600" : "#ffffff";
      context.fillText(word.text, x, baseline);
      x += widths[index];
    });
    baseline += lineHeight;
  }
  context.shadowColor = "transparent";
  context.shadowBlur = 0;
}

async function drawMuPhoto(
  context: CanvasRenderingContext2D,
  slide: CarouselOutlineSlide,
  rawUrl: string,
  slideNumber: number,
  y: number,
  height: number
) {
  const loaded = await loadExportImage(slideImageUrl(slide, rawUrl), slideNumber);
  try {
    drawCoverImage(
      context,
      loaded.image,
      0,
      y,
      CAROUSEL_EXPORT_WIDTH,
      height,
      slide.focal_x,
      slide.focal_y
    );
  } finally {
    loaded.release();
  }
}

async function renderMuEventPhoto(
  context: CanvasRenderingContext2D,
  slide: CarouselOutlineSlide,
  slideNumber: number,
  slideIndex: number,
  options: CarouselRenderOptions
) {
  const text = (slide.transcript_text || slide.hook_line || slide.caption || "").trim();
  const primaryUrl = slide.preview_url?.trim() || "";
  if (slideIndex === 0) {
    if (primaryUrl) {
      await drawMuPhoto(context, slide, primaryUrl, slideNumber, 0, CAROUSEL_EXPORT_HEIGHT);
      drawMuFade(context, 0, CAROUSEL_EXPORT_HEIGHT);
    }
    drawCenteredCaption(context, slide, text, 1035, 4, 82);
    const credit =
      (options.speakerName || slide.source_metadata?.source_name || options.title || "").trim();
    if (credit) {
      context.fillStyle = "#ffffff";
      context.textAlign = "center";
      context.textBaseline = "alphabetic";
      setFont(context, 32, 560);
      context.fillText(credit, CAROUSEL_EXPORT_WIDTH / 2, 1232);
      context.textAlign = "left";
    }
    return;
  }

  const panelPair = usableSplitPanels(slide);
  const secondary = options.secondarySlide;
  const secondaryUrl = panelPair
    ? panelPair[1].preview_url!.trim()
    : secondary?.preview_url?.trim() &&
        imageIdentity(secondary.preview_url) !== imageIdentity(primaryUrl)
      ? secondary.preview_url.trim()
      : "";
  const topUrl = panelPair ? panelPair[0].preview_url!.trim() : primaryUrl;
  if (topUrl && secondaryUrl) {
    const bandHeight = 214;
    const photoHeight = (CAROUSEL_EXPORT_HEIGHT - bandHeight) / 2;
    await drawMuPhoto(context, slide, topUrl, slideNumber, 0, photoHeight);
    await drawMuPhoto(
      context,
      panelPair?.[1] ? { ...slide, ...panelPair[1], preview_url: secondaryUrl } : secondary || slide,
      secondaryUrl,
      slideNumber,
      photoHeight + bandHeight,
      photoHeight
    );
    drawMuFade(context, 0, photoHeight, true, false);
    drawMuFade(context, photoHeight + bandHeight, photoHeight, false, true);
    context.fillStyle = "#050505";
    context.fillRect(0, photoHeight, CAROUSEL_EXPORT_WIDTH, bandHeight);
    drawCenteredCaption(context, slide, text, photoHeight + bandHeight / 2, 2, 54);
    return;
  }

  if (primaryUrl || topUrl) {
    await drawMuPhoto(context, slide, primaryUrl || topUrl, slideNumber, 0, CAROUSEL_EXPORT_HEIGHT);
    drawMuFade(context, 0, CAROUSEL_EXPORT_HEIGHT);
    drawCenteredCaption(context, slide, text, 1120, 3, 60);
    return;
  }
  drawCenteredCaption(context, slide, text, CAROUSEL_EXPORT_HEIGHT / 2, 5, 72);
}

function canvasToJpeg(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (blob) resolve(blob);
        else reject(new CarouselExportError("The browser could not encode the slide as JPEG."));
      },
      "image/jpeg",
      CAROUSEL_JPEG_QUALITY
    );
  });
}

export async function renderCarouselSlideToCanvas(
  slide: CarouselOutlineSlide,
  layout: CarouselExportLayout,
  slideIndex: number,
  slideCount: number,
  options: CarouselRenderOptions = {}
): Promise<HTMLCanvasElement> {
  const slideNumber = slideIndex + 1;
  validateSlideForExport(slide, layout, slideNumber, options.preset);
  const canvas = document.createElement("canvas");
  canvas.width = CAROUSEL_EXPORT_WIDTH;
  canvas.height = CAROUSEL_EXPORT_HEIGHT;
  const context = canvas.getContext("2d");
  if (!context) {
    throw new CarouselExportError("Canvas rendering is unavailable in this browser.");
  }
  context.fillStyle = "#111111";
  context.fillRect(0, 0, canvas.width, canvas.height);

  if (options.preset === "mu_event_photo") {
    await renderMuEventPhoto(context, slide, slideNumber, slideIndex, options);
  } else if (layout === "split_2") {
    await renderSplit(context, slide, slideNumber);
  } else {
    const role = slideIndex === 0 ? "cover" : slideIndex === slideCount - 1 ? "final" : "body";
    await renderSingle(context, slide, slideNumber, role);
  }
  if (options.preset !== "mu_event_photo") {
    drawSlideNumber(context, slideIndex, slideCount);
  }
  return canvas;
}

export async function renderCarouselSlide(
  slide: CarouselOutlineSlide,
  layout: CarouselExportLayout,
  slideIndex: number,
  slideCount: number,
  options: CarouselRenderOptions = {}
): Promise<Blob> {
  const canvas = await renderCarouselSlideToCanvas(
    slide,
    layout,
    slideIndex,
    slideCount,
    options
  );
  return canvasToJpeg(canvas);
}

export async function renderCarouselSlidePreviewUrl(
  slide: CarouselOutlineSlide,
  layout: CarouselExportLayout,
  slideIndex: number,
  slideCount: number,
  options: CarouselRenderOptions = {}
): Promise<string> {
  const blob = await renderCarouselSlide(slide, layout, slideIndex, slideCount, options);
  return URL.createObjectURL(blob);
}
