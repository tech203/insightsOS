# Shared classifier — kept as the single source of truth so the
# three call sites (generate_brand_kit, build_business_context in
# website_page_builder.py, build_demo_website_blueprint in app.py)
# can't drift in their keyword lists. The canonical buckets returned
# here are mapped to per-function theme names by the callers.
INDUSTRY_KEYWORDS = {
    "food_and_beverage": [
        "ice cream", "dessert", "confectionery", "food", "cafe",
        "restaurant", "bakery", "f&b", "beverage",
    ],
    "clinic": [
        "dental", "clinic", "doctor", "medical", "aesthetic",
        "health", "wellness",
    ],
    "education": [
        "tuition", "education", "enrichment", "school", "learning",
        "psle", "math", "english",
    ],
    "ecommerce": [
        "ecommerce", "e-commerce", "retail", "shop", "online store",
        "merchandise", "products",
    ],
}


def classify_industry_theme(industry="", services=""):
    """Return a canonical bucket (food_and_beverage / clinic /
    education / ecommerce / general) for the given industry + services
    text. Buckets are evaluated in INDUSTRY_KEYWORDS order — the first
    bucket whose keyword appears in the lowercased combined text wins.

    Callers map the canonical bucket to their own internal theme name
    (e.g. clinic → clinic_wellness in the demo blueprint, clinic
    everywhere else). Keeping the keyword lists in ONE place means
    a new industry word (say "physio") only has to be added once."""
    combined = f"{industry} {services}".lower()
    for bucket, keywords in INDUSTRY_KEYWORDS.items():
        if any(keyword in combined for keyword in keywords):
            return bucket
    return "general"


# Per-theme palette variants. Index 0 is the default the original
# generate_brand_kit() returned; variants 1-3 give the "Regenerate
# Palette" button something real to do. Each is hand-picked to fit
# the industry while feeling visibly different from the others.
PALETTE_VARIANTS = {
    "food_and_beverage": [
        # 0: warm orange (default — bakery / dessert energy)
        ("#f97316", "#fff7ed", "#fed7aa"),
        # 1: rose pink (premium / patisserie feel)
        ("#e11d48", "#fff1f2", "#fecdd3"),
        # 2: emerald (organic / artisan)
        ("#059669", "#ecfdf5", "#a7f3d0"),
        # 3: amber gold (caramel / nostalgic)
        ("#d97706", "#fffbeb", "#fde68a"),
    ],
    "clinic": [
        # 0: trust blue (default)
        ("#2563eb", "#eff6ff", "#bfdbfe"),
        # 1: teal (modern wellness)
        ("#0d9488", "#f0fdfa", "#99f6e4"),
        # 2: indigo (premium aesthetic)
        ("#6366f1", "#eef2ff", "#c7d2fe"),
        # 3: rose (gentle / patient-friendly)
        ("#e11d48", "#fff1f2", "#fecdd3"),
    ],
    "education": [
        # 0: purple (default — encouraging)
        ("#7c3aed", "#f5f3ff", "#ddd6fe"),
        # 1: amber gold (academic warmth)
        ("#d97706", "#fffbeb", "#fde68a"),
        # 2: emerald (growth / progress)
        ("#059669", "#ecfdf5", "#a7f3d0"),
        # 3: rose (kids / playful)
        ("#db2777", "#fdf2f8", "#fbcfe8"),
    ],
    "general": [
        # 0: indigo (default professional)
        ("#4f46e5", "#eef2ff", "#c7d2fe"),
        # 1: emerald (modern services)
        ("#059669", "#ecfdf5", "#a7f3d0"),
        # 2: rose (premium consultancy)
        ("#be185d", "#fdf2f8", "#fbcfe8"),
        # 3: slate (enterprise / fintech)
        ("#0f172a", "#f1f5f9", "#cbd5e1"),
    ],
}


# Buckets that share a palette family — keeps PALETTE_VARIANTS
# concise without sacrificing the right colour cycling.
_PALETTE_ALIASES = {"ecommerce": "food_and_beverage"}


def get_palette_variant(industry_theme, variant_index):
    """Return (primary, secondary, accent) for the given theme +
    variant. Falls back to the general bucket and clamps the index.

    Used by the "Regenerate Palette" button to cycle through visibly
    different palettes for the same industry, without re-running
    generate_brand_kit and losing other state (personality, visual
    style, etc.)."""
    canonical = _PALETTE_ALIASES.get(industry_theme, industry_theme)
    variants = PALETTE_VARIANTS.get(canonical) or PALETTE_VARIANTS["general"]
    if not variants:
        variants = PALETTE_VARIANTS["general"]
    return variants[variant_index % len(variants)]


def generate_brand_kit(
    business_name="",
    industry="",
    location="",
    services="",
    business_context=None,
):
    business_context = business_context or {}

    bucket = classify_industry_theme(industry, services)

    # Per-bucket shape. Was 4 hand-rolled `if any(word in combined…)`
    # branches; now driven by the shared classifier so adding a
    # keyword updates all three call sites at once.
    bucket_to_kit = {
        "food_and_beverage": {
            "industry_theme": "food_and_beverage",
            "personality": ["playful", "warm", "nostalgic", "friendly"],
            "tone_of_voice": "friendly, appetising, simple, inviting",
            "visual_style": "modern dessert brand",
            "primary_color": "#f97316",
            "secondary_color": "#fff7ed",
            "accent_color": "#fed7aa",
            "font_style": "rounded modern",
            "imagery_style": "bright product photography, desserts, lifestyle shots",
            "primary_cta": "Shop Now",
            "secondary_cta": "View Products",
            "hero_direction": "Warm ecommerce hero with product showcase",
        },
        "ecommerce": {
            "industry_theme": "ecommerce",
            "personality": ["clear", "product-led", "trustworthy", "modern"],
            "tone_of_voice": "clear, product-focused, conversion-friendly",
            "visual_style": "modern ecommerce brand",
            "primary_color": "#f97316",
            "secondary_color": "#fff7ed",
            "accent_color": "#fed7aa",
            "font_style": "modern sans-serif",
            "imagery_style": "product-led photography, lifestyle context",
            "primary_cta": "Shop Now",
            "secondary_cta": "View Products",
            "hero_direction": "Product-focused ecommerce hero",
        },
        "clinic": {
            "industry_theme": "clinic",
            "personality": ["professional", "calm", "trustworthy", "reassuring"],
            "tone_of_voice": "clear, professional, reassuring",
            "visual_style": "clean healthcare brand",
            "primary_color": "#2563eb",
            "secondary_color": "#eff6ff",
            "accent_color": "#bfdbfe",
            "font_style": "clean modern",
            "imagery_style": "clinic environment, smiling patients, clean spaces",
            "primary_cta": "Book Appointment",
            "secondary_cta": "View Services",
            "hero_direction": "Trust-focused healthcare hero",
        },
        "education": {
            "industry_theme": "education",
            "personality": ["supportive", "clear", "encouraging", "parent-friendly"],
            "tone_of_voice": "helpful, encouraging, simple",
            "visual_style": "friendly education brand",
            "primary_color": "#7c3aed",
            "secondary_color": "#f5f3ff",
            "accent_color": "#ddd6fe",
            "font_style": "friendly modern",
            "imagery_style": "students, learning, classroom, progress",
            "primary_cta": "Enquire Now",
            "secondary_cta": "View Programmes",
            "hero_direction": "Parent-friendly education hero",
        },
        "general": {
            "industry_theme": "general",
            "personality": ["clear", "professional", "trustworthy"],
            "tone_of_voice": "clear, professional, simple",
            "visual_style": "clean business website",
            "primary_color": "#4f46e5",
            "secondary_color": "#eef2ff",
            "accent_color": "#c7d2fe",
            "font_style": "modern professional",
            "imagery_style": "business, people, service experience",
            "primary_cta": "Enquire Now",
            "secondary_cta": "View Services",
            "hero_direction": "Trust-focused business hero",
        },
    }

    kit = dict(bucket_to_kit[bucket])
    kit["brand_name"] = business_name
    kit["text_color"] = "#0f172a"
    kit["background_color"] = "#ffffff"
    return kit