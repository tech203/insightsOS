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


def get_palette_variant(industry_theme, variant_index):
    """Return (primary, secondary, accent) for the given theme +
    variant. Falls back to the general bucket and clamps the index.

    Used by the "Regenerate Palette" button to cycle through visibly
    different palettes for the same industry, without re-running
    generate_brand_kit and losing other state (personality, visual
    style, etc.)."""
    variants = PALETTE_VARIANTS.get(industry_theme) or PALETTE_VARIANTS["general"]
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

    combined = f"{industry} {services}".lower()

    if any(word in combined for word in ["ice cream", "dessert", "confectionery", "food", "cafe", "restaurant", "bakery", "f&b"]):
        return {
            "brand_name": business_name,
            "industry_theme": "food_and_beverage",
            "personality": ["playful", "warm", "nostalgic", "friendly"],
            "tone_of_voice": "friendly, appetising, simple, inviting",
            "visual_style": "modern dessert brand",
            "primary_color": "#f97316",
            "secondary_color": "#fff7ed",
            "accent_color": "#fed7aa",
            "text_color": "#0f172a",
            "background_color": "#ffffff",
            "font_style": "rounded modern",
            "imagery_style": "bright product photography, desserts, lifestyle shots",
            "primary_cta": "Shop Now",
            "secondary_cta": "View Products",
            "hero_direction": "Warm ecommerce hero with product showcase",
        }

    if any(word in combined for word in ["dental", "clinic", "medical", "health", "aesthetic"]):
        return {
            "brand_name": business_name,
            "industry_theme": "clinic",
            "personality": ["professional", "calm", "trustworthy", "reassuring"],
            "tone_of_voice": "clear, professional, reassuring",
            "visual_style": "clean healthcare brand",
            "primary_color": "#2563eb",
            "secondary_color": "#eff6ff",
            "accent_color": "#bfdbfe",
            "text_color": "#0f172a",
            "background_color": "#ffffff",
            "font_style": "clean modern",
            "imagery_style": "clinic environment, smiling patients, clean spaces",
            "primary_cta": "Book Appointment",
            "secondary_cta": "View Services",
            "hero_direction": "Trust-focused healthcare hero",
        }

    if any(word in combined for word in ["tuition", "education", "enrichment", "school", "learning"]):
        return {
            "brand_name": business_name,
            "industry_theme": "education",
            "personality": ["supportive", "clear", "encouraging", "parent-friendly"],
            "tone_of_voice": "helpful, encouraging, simple",
            "visual_style": "friendly education brand",
            "primary_color": "#7c3aed",
            "secondary_color": "#f5f3ff",
            "accent_color": "#ddd6fe",
            "text_color": "#0f172a",
            "background_color": "#ffffff",
            "font_style": "friendly modern",
            "imagery_style": "students, learning, classroom, progress",
            "primary_cta": "Enquire Now",
            "secondary_cta": "View Programmes",
            "hero_direction": "Parent-friendly education hero",
        }

    return {
        "brand_name": business_name,
        "industry_theme": "general",
        "personality": ["clear", "professional", "trustworthy"],
        "tone_of_voice": "clear, professional, simple",
        "visual_style": "clean business website",
        "primary_color": "#4f46e5",
        "secondary_color": "#eef2ff",
        "accent_color": "#c7d2fe",
        "text_color": "#0f172a",
        "background_color": "#ffffff",
        "font_style": "modern professional",
        "imagery_style": "business, people, service experience",
        "primary_cta": "Enquire Now",
        "secondary_cta": "View Services",
        "hero_direction": "Trust-focused business hero",
    }