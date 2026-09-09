"""Qwen identify labels: specific searchable tags, not generic body parts."""

from __future__ import annotations

import re

IDENTIFY_PROMPT = """You are labeling a photo for a visual search index.

Return sections in this order. Each line is: primary label | synonym, synonym, synonym
Synonyms are other phrases a person might type to find the same thing.

SCENE (required when the place is identifiable — one or two lines)
The setting, not empty wall/floor/sky:
- restaurant interior | buffet, cafeteria, dining hall
- cafe counter | food stall, coffee shop
- event photo backdrop | stage, gym, workshop

OBJECTS (required)
Specific nouns a person could search for, including props behind people and oversized set pieces:
- graduation cap | mortarboard, academic cap, mortar board
- menu board | restaurant menu, food menu
- garments with color and type, e.g. black shirt | dark tee, black t-shirt
- brand, logo, or sign text copied exactly, e.g. Flourish Foods | flourish
- named objects, e.g. ceremonial cheque | check, oversized check, award cheque
- people as a short description, e.g. man with beard | bearded man
Do not skip a graduation cap, mortarboard, hat, or restaurant/cafe because it hangs above people, is oversized, or sits behind them.
Prefer the place and distinctive props over listing every plate, glass, or packet.

ACTIONS
Visible acts as verb + object/scene, never a bare gerund:
- giving ceremonial cheque | handing cheque, presenting cheque, awarding cheque
- wearing navy blazer | dressed in navy blazer
- cooking at stove | chopping vegetables, preparing food
- speaking on stage | presenting on stage, giving a talk
- dining in restaurant | eating at buffet, sitting in cafe
- posing with graduation cap | holding mortarboard

Never list isolated body parts: eye, mouth, nose, ear, hair, head, face, teeth, skin, smile, hand, arm, leg, foot.
Never list empty scene filler: wall, floor, ceiling, sky, light, shadow.
Never list a generic background, text, logo, or sign unless the words are readable or the object is specific (graduation cap, menu board, 3D letters).
Never list a bare verb alone: holding, wearing, sitting, smiling, standing.

No bounding boxes. No sentences. Skip anything you cannot identify specifically."""

CAPTION_PROMPT = """Write a detailed factual caption for visual search (text embedding + keyword match). Prefer completeness over brevity: 4-7 sentences, about 80-140 words.

Cover every searchable fact you can see:
1. People: count, gender/age look, notable appearance; each main garment as color + type (black t-shirt, navy blazer, yellow stole); any print or logo on clothing and where it sits (chest, cap, shorts).
2. Actions: what each person is doing as verb + object (speaking on stage, posing with graduation cap, handing ceremonial cheque, eating at buffet). Include posing, wearing, holding.
3. Place: restaurant interior, cafe, buffet, food stall, kitchen, gym, stage, event photo backdrop, classroom, street — even when it sits behind or between people.
4. Distinctive props anywhere in frame, including hanging, oversized, or background: graduation cap/mortarboard, menu board, ceremonial cheque, trophy, microphone, diploma, backdrop lettering.
5. Readable text: copy sign, banner, jersey, and brand wording exactly in quotes (for example "masters' union", "HYROX DELHI", "CLASS OF HYROX'26"). Skip fragments you cannot read fully.

Use concrete phrases a person would type to find this photo. Name background objects, not only the foreground person. Do not write "the image shows". Do not guess names, mood, unreadable brands, or places you cannot see. Output caption sentences only — no headings, lists, or JSON."""

# Mixed lane: earlier identify contract (SCENE/OBJECTS/ACTIONS + examples) plus a detailed search caption.
IDENTIFY_AND_CAPTION_PROMPT = """Label this photo for visual search. Output exactly these headings in this order, nothing else:

SCENE
primary | synonym, synonym
OBJECTS
primary | synonym, synonym
ACTIONS
verb + object | synonym, synonym
CAPTION
detailed sentences

Each tag line is a primary search phrase | other phrases a person might type.

SCENE (required when the place is identifiable — one or two lines). The setting, not empty wall/floor/sky, even if it is behind people:
- restaurant interior | buffet, cafeteria, dining hall
- cafe counter | food stall, coffee shop
- event photo backdrop | stage, gym, workshop

OBJECTS (required). Specific nouns a person could search for, including props behind people and oversized set pieces:
- graduation cap | mortarboard, academic cap, mortar board
- menu board | restaurant menu, food menu
- garments with color and type, e.g. black shirt | dark tee, black t-shirt
- brand, logo, or sign text copied exactly, e.g. Flourish Foods | flourish
- named objects, e.g. ceremonial cheque | check, oversized check, award cheque
- people as a short description, e.g. man with beard | bearded man
Do not skip a graduation cap, mortarboard, hat, or restaurant/cafe because it hangs above people, is oversized, or sits behind them.
Prefer the place and distinctive props over listing every plate, glass, or packet.

ACTIONS. Required whenever anyone is doing anything, including posing or wearing. Visible acts as verb + object, never a bare gerund, never under OBJECTS:
- giving ceremonial cheque | handing cheque, presenting cheque, awarding cheque
- wearing navy blazer | dressed in navy blazer
- cooking at stove | chopping vegetables, preparing food
- speaking on stage | presenting on stage, giving a talk
- dining in restaurant | eating at buffet, sitting in cafe
- posing with graduation cap | holding mortarboard

Never list isolated body parts: eye, mouth, nose, ear, hair, head, face, teeth, skin, smile, hand, arm, leg, foot.
Never list empty scene filler: wall, floor, ceiling, sky, light, shadow.
Never list a generic background, text, logo, or sign unless the words are readable or the object is specific (graduation cap, menu board, 3D letters).
Never list a bare verb alone: holding, wearing, sitting, smiling, standing.

CAPTION: 4-7 factual sentences (~80-140 words) for embedding search. Repeat the same people, garment color+type+print, actions, place, distinctive props, and quoted readable signage as the tags. Include hanging and background objects, not only the foreground. No "image shows", no speculation, no stray OCR fragments."""

JUNK = {
    "remove",
    "reveal",
    "extrude",
    "assemble",
    "modern",
    "lesson",
    "clip art",
    "team presentation",
    "press room",
    "number icon",
    "frame",
    "head",
    "smile",
    "hair",
    "sun",
    "sky",
    "horizon",
    "sunlight",
    "eye",
    "eyes",
    "mouth",
    "nose",
    "ear",
    "ears",
    "teeth",
    "tooth",
    "lip",
    "lips",
    "skin",
    "face",
    "eyebrow",
    "eyebrows",
    "cheek",
    "cheeks",
    "chin",
    "forehead",
    "neck",
    "hand",
    "hands",
    "finger",
    "fingers",
    "arm",
    "arms",
    "leg",
    "legs",
    "foot",
    "feet",
    "wrist",
    "palm",
    "wall",
    "walls",
    "floor",
    "ceiling",
    "light",
    "lights",
    "lighting",
    "shadow",
    "shadows",
    "background",
    "black background",
    "white background",
    "horizon",
    "sky",
    "sun",
    "sleeve",
    "sleeves",
    "collar",
    "button",
    "buttons",
    "hem",
    "stitch",
    "thread",
    "fabric",
    "cotton",
    "denim",
    "pocket",
    "pockets",
    "zipper",
    "hole",
    "sign",
    "logo",
    "text",
    "paper",
    "shirt",
    "t-shirt",
    "tshirt",
    "tee",
    "pants",
    "jeans",
    "jacket",
    "shorts",
    "clothes",
    "clothing",
    "garment",
    "apparel",
    "chair",
    "table",
    "window",
    "door",
    "screen",
    "banner",
    "poster",
    "bag",
    "cup",
    "person",
    "people",
    "man",
    "woman",
    "boy",
    "girl",
    "image",
    "photo",
    "picture",
}

_COLORS = {
    "black",
    "white",
    "red",
    "blue",
    "navy",
    "green",
    "yellow",
    "pink",
    "brown",
    "beige",
    "grey",
    "gray",
    "orange",
    "purple",
    "gold",
    "silver",
}

_VERBISH = re.compile(
    r"^(sitting|standing|wearing|holding|looking|smiling|walking|running|"
    r"posing|showing|using|playing)$"
)
_LINE = re.compile(r"^[*\-•\d.\s]+")
_BODY_WORD = re.compile(
    r"\b(eye|eyes|mouth|nose|ear|ears|teeth|lips?|skin|face|hair|head|"
    r"smile|cheek|chin|forehead|neck|hands?|fingers?|arms?|legs?|feet|foot)\b"
)


def filter_tags(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = _LINE.sub("", raw).strip().strip("*").strip()
        if not line or line.startswith("```"):
            continue
        label = re.sub(r"\s+", " ", line).strip(" .,:;").casefold()
        if not label or len(label) > 48:
            continue
        if any(ch.isdigit() for ch in label) and re.search(r"\d{2,}", label):
            continue
        if label in JUNK or _VERBISH.match(label) or label in _COLORS:
            continue
        if _BODY_WORD.search(label) and len(label.split()) <= 2:
            continue
        if label in seen:
            continue
        seen.add(label)
        out.append(label)
    return out[:24]
